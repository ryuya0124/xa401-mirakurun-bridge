//! Native loopback API and bounded, same-channel shared receiving sessions.
use crate::{
    crypto::Credentials,
    device::{Config, Device, Item},
    receiver,
};
use anyhow::{Result, ensure};
use axum::{
    Json, Router,
    body::Body,
    extract::{Path, Query, Request, State},
    http::StatusCode,
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
};
use bytes::Bytes;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{
        Arc, Weak,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::{Mutex, broadcast};
type Shared = Arc<App>;
struct App {
    config: Config,
    device: Mutex<Device>,
    credentials: Arc<Credentials>,
    sessions: Mutex<HashMap<String, Weak<Session>>>,
    recordings: Mutex<(u64, Vec<Item>)>,
    ake: Arc<Mutex<()>>,
    epg: Arc<std::sync::Mutex<crate::cache::Cache>>,
}
struct Session {
    key: String,
    tx: broadcast::Sender<Bytes>,
    abort: tokio::task::AbortHandle,
    bytes: Arc<AtomicU64>,
    started: u64,
}
impl Drop for Session {
    fn drop(&mut self) {
        self.abort.abort();
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
type ApiResult<T> = std::result::Result<T, (StatusCode, &'static str)>;
fn bad(_: anyhow::Error) -> (StatusCode, &'static str) {
    (
        StatusCode::BAD_GATEWAY,
        "XA401 operation failed; sensitive details suppressed",
    )
}
async fn boundary(req: Request, next: Next) -> Response {
    let host = req
        .headers()
        .get("host")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let valid = url::Url::parse(&format!("http://{host}"))
        .ok()
        .is_some_and(|u| {
            u.username().is_empty()
                && u.password().is_none()
                && u.path() == "/"
                && u.host_str().is_some_and(|h| {
                    h == "localhost"
                        || h.parse::<std::net::IpAddr>()
                            .is_ok_and(|ip| ip.is_loopback())
                })
        });
    if !valid {
        return (StatusCode::MISDIRECTED_REQUEST, "Loopback Host required").into_response();
    }
    let h = req.headers();
    if h.get("sec-fetch-site").is_some_and(|v| v == "cross-site")
        || h.get("origin")
            .is_some_and(|v| v.to_str().ok() != Some(format!("http://{host}").as_str()))
    {
        return (StatusCode::FORBIDDEN, "Cross-origin request refused").into_response();
    }
    next.run(req).await
}
impl App {
    async fn recordings(&self) -> Result<Vec<Item>> {
        let mut cache = self.recordings.lock().await;
        if now().saturating_sub(cache.0) > 60 {
            cache.1 = self.device.lock().await.recordings().await?;
            cache.0 = now();
        }
        Ok(cache.1.clone())
    }
    fn programs(&self) -> Vec<Value> {
        self.epg
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .programs()
    }
    async fn subscribe(
        &self,
        key: String,
        object_id: String,
        position: f64,
    ) -> ApiResult<(Arc<Session>, broadcast::Receiver<Bytes>)> {
        let mut sessions = self.sessions.lock().await;
        sessions.retain(|_, v| v.strong_count() > 0);
        if let Some(s) = sessions.get(&key).and_then(Weak::upgrade) {
            let rx = s.tx.subscribe();
            return Ok((s, rx));
        }
        if sessions.len() >= self.config.max_streams {
            return Err((
                StatusCode::SERVICE_UNAVAILABLE,
                "All receive slots are in use",
            ));
        }
        let mut device = self.device.lock().await;
        let resource = device.resource(&object_id).await.map_err(bad)?;
        let host = device.host;
        let mut sit = device
            .items
            .iter()
            .find(|i| i.object_id == object_id)
            .and_then(|i| i.service.clone())
            .map(crate::epg::Parser::new);
        drop(device);
        let (tx, rx) = broadcast::channel(32);
        let publish = tx.clone();
        let (input, mut output) = tokio::sync::mpsc::channel(8);
        let credentials = self.credentials.clone();
        let source = self.config.source;
        let path = self.config.state_dir.join("dtcp/srm.bin");
        let bytes = Arc::new(AtomicU64::new(0));
        let metric = bytes.clone();
        let ake = self.ake.clone();
        let epg = self.epg.clone();
        let task = tokio::spawn(async move {
            // The authentication lock is released before reading the media body.
            let worker = tokio::spawn(async move {
                receiver::receive_locked(
                    source,
                    host,
                    &resource,
                    &credentials,
                    &path,
                    position,
                    input,
                    Some(ake),
                )
                .await
            });
            struct Cancel(tokio::task::AbortHandle);
            impl Drop for Cancel {
                fn drop(&mut self) {
                    self.0.abort();
                }
            }
            let _cancel = Cancel(worker.abort_handle());
            while let Some(chunk) = output.recv().await {
                if let Some(ref mut sit) = sit {
                    let programs = sit.feed(&chunk);
                    if !programs.is_empty() {
                        let mut cache = epg.lock().unwrap_or_else(|e| e.into_inner());
                        let mut changed = false;
                        for p in programs {
                            changed |= cache.update(p).unwrap_or(false);
                        }
                        if changed && cache.save().is_err() {
                            eprintln!("Observed EPG cache write failed");
                        }
                    }
                }
                metric.fetch_add(chunk.len() as u64, Ordering::Relaxed);
                if publish.send(chunk).is_err() {
                    break;
                }
            }
            if let Ok(Err(_)) = worker.await {
                eprintln!("Native receive session failed (details suppressed)");
            }
        });
        let s = Arc::new(Session {
            key: key.clone(),
            tx,
            abort: task.abort_handle(),
            bytes,
            started: now(),
        });
        sessions.insert(key, Arc::downgrade(&s));
        Ok((s, rx))
    }
}
async fn deliver(s: Arc<Session>, mut rx: broadcast::Receiver<Bytes>) -> ApiResult<Response> {
    let first = tokio::time::timeout(Duration::from_secs(35), rx.recv())
        .await
        .map_err(|_| (StatusCode::GATEWAY_TIMEOUT, "XA401 startup timeout"))?
        .map_err(|_| {
            (
                StatusCode::BAD_GATEWAY,
                "XA401 did not produce validated TS",
            )
        })?;
    let stream = futures_util::stream::unfold(
        (Some(first), rx, s),
        |(mut first, mut rx, guard)| async move {
            if let Some(chunk) = first.take() {
                return Some((Ok::<_, std::io::Error>(chunk), (None, rx, guard)));
            }
            match tokio::time::timeout(Duration::from_secs(20), rx.recv()).await {
                Ok(Ok(chunk)) => Some((Ok(chunk), (None, rx, guard))),
                _ => None,
            }
        },
    );
    Ok((
        [
            ("Content-Type", "video/MP2T"),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-XA401-Engine", "rust"),
        ],
        Body::from_stream(stream),
    )
        .into_response())
}
async fn live(State(a): State<Shared>, Path(id): Path<u64>) -> ApiResult<Response> {
    let object = a
        .device
        .lock()
        .await
        .items
        .iter()
        .find(|i| {
            i.service
                .as_ref()
                .is_some_and(|s| s["id"].as_u64() == Some(id))
        })
        .map(|i| i.object_id.clone())
        .ok_or((StatusCode::NOT_FOUND, "Unknown service"))?;
    let (s, rx) = a.subscribe(id.to_string(), object, 0.).await?;
    deliver(s, rx).await
}
async fn recorded(
    State(a): State<Shared>,
    Path(token): Path<String>,
    Query(q): Query<HashMap<String, String>>,
) -> ApiResult<Response> {
    let player = q.get("player").cloned().unwrap_or_default();
    let position = q
        .get("position")
        .map(|v| v.parse::<f64>())
        .transpose()
        .map_err(|_| (StatusCode::BAD_REQUEST, "Invalid position"))?
        .unwrap_or(0.);
    if token.len() != 24
        || !token
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        || player.len() != 32
        || !player
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        || !position.is_finite()
        || !(0. ..=86400.).contains(&position)
    {
        return Err((StatusCode::BAD_REQUEST, "Invalid playback request"));
    }
    let item = a
        .recordings()
        .await
        .map_err(bad)?
        .into_iter()
        .find(|i| i.token == token)
        .ok_or((StatusCode::NOT_FOUND, "Recording unavailable"))?;
    // Stop an old request from this player before acquiring its replacement slot.
    {
        let mut sessions = a.sessions.lock().await;
        let suffix = format!("#{player}");
        sessions.retain(|k, v| {
            if k.starts_with("recorded:") && k.ends_with(&suffix) {
                if let Some(s) = v.upgrade() {
                    s.abort.abort();
                }
                false
            } else {
                true
            }
        });
    }
    let (s, rx) = a
        .subscribe(
            format!("recorded:{token}@{position:.3}#{player}"),
            item.object_id,
            position,
        )
        .await?;
    deliver(s, rx).await
}
fn duration(text: Option<&str>) -> Option<f64> {
    let mut p = text?.split(':');
    let h = p.next()?.parse::<f64>().ok()?;
    let m = p.next()?.parse::<f64>().ok()?;
    let s = p.next()?.parse::<f64>().ok()?;
    let t = h * 3600. + m * 60. + s;
    if t.is_finite() && t > 0. && t <= 86400. {
        Some(t)
    } else {
        None
    }
}
async fn catalogue(State(a): State<Shared>) -> ApiResult<Json<Value>> {
    let recordings=a.recordings().await.map_err(bad)?.into_iter().map(|i|json!({"id":i.token,"title":i.title,"channel":i.channel,"date":i.date,"duration":i.duration,"duration_seconds":duration(i.duration.as_deref())})).collect::<Vec<_>>();
    let channels=a.device.lock().await.items.iter().filter_map(|i|i.service.as_ref()).map(|s|json!({"id":s["id"].as_u64().unwrap_or(0).to_string(),"title":s["name"],"channel":s["channel"]["type"]})).collect::<Vec<_>>();
    Ok(Json(
        json!({"protocol":1,"recordings":recordings,"channels":channels}),
    ))
}
async fn source(State(a): State<Shared>, Path(id): Path<u64>) -> ApiResult<Json<Value>> {
    let d = a.device.lock().await;
    let item = d
        .items
        .iter()
        .find(|i| {
            i.service
                .as_ref()
                .is_some_and(|s| s["id"].as_u64() == Some(id))
        })
        .ok_or((StatusCode::NOT_FOUND, "Unknown service"))?;
    Ok(Json(
        json!({"kind":"live","id":id.to_string(),"title":item.service.as_ref().unwrap()["name"],"duration":null,"stream_url":format!("/xa401/api/native/channels/{id}/stream")}),
    ))
}
async fn recorded_source(
    State(a): State<Shared>,
    Path(token): Path<String>,
) -> ApiResult<Json<Value>> {
    let item = a
        .recordings()
        .await
        .map_err(bad)?
        .into_iter()
        .find(|i| i.token == token)
        .ok_or((StatusCode::NOT_FOUND, "Recording unavailable"))?;
    let mut d = a.device.lock().await;
    let resource = d.resource(&item.object_id).await.map_err(bad)?;
    let head = d
        .http
        .head(&resource.url)
        .header("TimeSeekRange.dlna.org", "npt=0.000-")
        .header("transferMode.dlna.org", "Streaming")
        .header("getcontentFeatures.dlna.org", "1")
        .header("Accept-Encoding", "identity")
        .send()
        .await
        .map_err(|_| (StatusCode::BAD_GATEWAY, "Duration probe failed"))?;
    let seconds = head
        .headers()
        .get("TimeSeekRange.dlna.org")
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.split('/').nth(1))
        .and_then(|s| s.split_whitespace().next())
        .and_then(|s| s.parse::<f64>().ok())
        .filter(|s| s.is_finite() && *s > 0. && *s <= 86400.)
        .ok_or((StatusCode::BAD_GATEWAY, "Precise duration unavailable"))?;
    Ok(Json(
        json!({"kind":"recorded","id":token,"title":item.title,"duration":seconds,"stream_url":format!("/xa401/api/recorded/{token}/stream")}),
    ))
}
async fn guide(State(a): State<Shared>) -> Json<Value> {
    let programs = a.programs();
    let channels=a.device.lock().await.items.iter().filter_map(|i|i.service.as_ref()).map(|s|{
        let program=programs.iter().find(|p|p["networkId"]==s["networkId"]&&p["serviceId"]==s["serviceId"]).map(|p|json!({"event_id":p["eventId"],"title":p["name"],"description":p["description"],"extended":p["extended"],"start_at":p["startAt"],"duration":p["duration"]}));
        json!({"id":s["id"].as_u64().unwrap_or(0).to_string(),"name":s["name"],"type":s["channel"]["type"],"number":s["remoteControlKeyId"],"program":program})
    }).collect::<Vec<_>>();
    Json(
        json!({"protocol":1,"generated_at":now()*1000,"source":"observed_SIT","future_schedule_available":false,"channels":channels}),
    )
}
async fn health(State(a): State<Shared>) -> Json<Value> {
    let streams=a.sessions.lock().await.values().filter_map(Weak::upgrade).map(|s|json!({"service_id":s.key,"ts_bytes":s.bytes.load(Ordering::Relaxed),"clients":s.tx.receiver_count(),"started_at":s.started})).collect::<Vec<_>>();
    Json(
        json!({"status":"ok","engine":"rust","services":a.device.lock().await.items.len(),"streams":streams,"maximum_distinct_channels":a.config.max_streams,"cached_programs":a.programs().len(),"epg":"observed_SIT"}),
    )
}
pub async fn serve(config: Config) -> Result<()> {
    ensure!(
        config.listen.is_loopback(),
        "Only loopback API binding is permitted"
    );
    let credentials = Arc::new(Credentials::load(&config.credentials)?);
    let mut device = Device::new(config.clone())?;
    device.refresh().await?;
    let bind = (config.listen, config.port);
    let epg = Arc::new(std::sync::Mutex::new(crate::cache::Cache::load(
        &config.state_dir,
        &config.udn,
    )));
    let a = Arc::new(App {
        config,
        device: Mutex::new(device),
        credentials,
        sessions: Mutex::new(HashMap::new()),
        recordings: Mutex::new((0, vec![])),
        ake: Arc::new(Mutex::new(())),
        epg,
    });
    let app=Router::new()
        .route("/health",get(health))
        .route("/api/services",get(|State(a):State<Shared>|async move{Json(a.device.lock().await.items.iter().filter_map(|i|i.service.clone()).collect::<Vec<_>>())}))
        .route("/api/programs",get(|State(a):State<Shared>|async move{Json(a.programs())}))
        .route("/api/tuners",get(|State(a):State<Shared>|async move{let count=a.sessions.lock().await.values().filter(|s|s.strong_count()>0).count();Json((0..a.config.max_streams).map(|i|json!({"index":i,"name":format!("XA401 receive slot {}",i+1),"types":["GR","BS","CS","CATV"],"isAvailable":true,"isFree":i>=count,"isRemote":false,"users":if i<count{vec![json!({"id":format!("xa401-native-{i}"),"priority":0})]}else{vec![]}})).collect::<Vec<_>>())}))
        .route("/api/version",get(||async{Json(json!({"current":env!("CARGO_PKG_VERSION"),"name":"xa401-mirakurun-bridge","engine":"rust","compatibleApi":"Mirakurun subset"}))}))
        .route("/api/services/{id}/stream",get(live))
        .route("/xa401/api/native/catalogue",get(catalogue))
        .route("/xa401/api/native/guide",get(guide))
        .route("/xa401/api/native/channels/{id}",get(source))
        .route("/xa401/api/native/channels/{id}/stream",get(live))
        .route("/xa401/api/native/recordings/{token}",get(recorded_source))
        .route("/xa401/api/recorded/{token}/stream",get(recorded))
        .layer(middleware::from_fn(boundary)).with_state(a);
    axum::serve(tokio::net::TcpListener::bind(bind).await?, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
