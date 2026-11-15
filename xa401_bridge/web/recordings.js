import { Mpeg2TsPlayer } from "/xa401/player-lib/index.js";
const $ = (id) => document.getElementById(id);
let state = {},
  loaded = false;
const names = {
  scheduled: "予約済み",
  waiting: "受信枠待ち",
  recording: "録画中",
  finalizing: "保存・検証中",
  stopping: "停止処理中",
  completed: "保存完了",
  stopped: "停止・保存済み",
  cancelled: "予約取消",
  failed: "失敗",
  interrupted: "再起動等で中断",
};
function node(tag, text) {
  let n = document.createElement(tag);
  n.textContent = text;
  return n;
}
async function api(path, data) {
  let r = await fetch("/xa401/api/" + path, {
    cache: "no-store",
    ...(data
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }
      : {}),
  });
  if (!r.ok) throw Error(await r.text());
  return await r.json();
}
function message(text) {
  $("message").textContent = text;
}
function titleFor(c) {
  let now = Date.now();
  let p = (state.programs || []).find(
    (p) =>
      p.networkId * 100000 + p.serviceId === c.id &&
      p.startAt <= now &&
      now < p.startAt + p.duration,
  );
  return p ? p.name : c.title;
}
function selectTitle() {
  let c = (state.channels || []).find(
    (c) => c.id === Number($("channel").value),
  );
  if (c) $("title").value = titleFor(c);
}
async function refresh() {
  try {
    state = await api("recording");
    if (!loaded) {
      for (let c of state.channels) {
        let o = node("option", c.name);
        o.value = c.id;
        $("channel").append(o);
      }
      selectTitle();
      loaded = true;
    }
    if (!state.enabled)
      message("PC録画はサーバー側でまだ有効になっていません。");
    renderJobs();
  } catch (e) {
    message(e.message);
  }
}
function renderJobs() {
  $("job-list").replaceChildren();
  for (let j of state.jobs) {
    let card = node("div", "");
    card.className = "card";
    card.append(
      node("strong", j.title),
      node(
        "p",
        `${names[j.status] || j.status} · ${new Date(j.start_at * 1000).toLocaleString()} · ${(j.bytes / 1024 ** 2).toFixed(1)} MiB`,
      ),
    );
    if (["scheduled", "waiting", "recording", "stopping"].includes(j.status)) {
      let b = node(
        "button",
        j.status === "scheduled" ? "予約を取り消す" : "停止して保存",
      );
      b.onclick = async () => {
        try {
          await api("jobs/" + j.id + "/stop", {});
          await refresh();
        } catch (e) {
          message(e.message);
        }
      };
      card.append(b);
    }
    if (["completed", "stopped"].includes(j.status)) {
      let a = node("a", "TSファイルを保存");
      a.href = "/xa401/api/jobs/" + j.id + "/file";
      a.download = j.file_name;
      card.append(
        a,
        node(
          "p",
          "KonomiTVへの反映に少し時間がかかることがあります。1分未満の動画はKonomiTVの対象外です。",
        ),
      );
    }
    if (j.error_type)
      card.append(
        node(
          "p",
          "処理に失敗しました。受信枠・空き容量・ローカルログを確認してください。",
        ),
      );
    $("job-list").append(card);
  }
}
$("channel").onchange = selectTitle;
$("live").onsubmit = async (e) => {
  e.preventDefault();
  try {
    let d = {
      source: "live",
      service_id: Number($("channel").value),
      title: $("title").value,
      duration_seconds: Math.round(Number($("minutes").value) * 60),
    };
    if ($("when").value) d.start_at = new Date($("when").value).toISOString();
    await api("jobs", d);
    message("録画・予約を登録しました。");
    await refresh();
    location.hash = "jobs";
  } catch (e) {
    message(e.message);
  }
};
let player = null,
  selected = null,
  playerId = crypto.randomUUID().replaceAll("-", "");
async function playRecorded(r, position = 0) {
  if (player) player.destroy();
  selected = r;
  $("playback").hidden = false;
  $("playing-title").textContent = r.title;
  $("position").value = (position / 60).toFixed(1);
  player = new Mpeg2TsPlayer($("video"), {
    mediaSource: "auto",
    passthrough: false,
  });
  player.addEventListener("error", (e) =>
    message("再生エラー: " + e.detail.error.message),
  );
  try {
    message("XA401に接続しています…");
    await player.load(
      `/xa401/api/recorded/${r.id}/stream?player=${playerId}&position=${position}`,
    );
    message("XA401から直接再生中");
    await $("video").play();
  } catch (e) {
    message(
      "再生を開始できませんでした。画面の再生ボタン、または再生のやり直しをお試しください。",
    );
  }
}
$("seek").onclick = () => {
  if (selected)
    playRecorded(selected, Math.max(0, Number($("position").value) * 60));
};
$("stop-player").onclick = () => {
  if (player) player.destroy();
  player = null;
  $("playback").hidden = true;
};
window.addEventListener("pagehide", () => player?.destroy());
$("load-recorded").onclick = async () => {
  try {
    let items = await api("recorded");
    $("recorded").replaceChildren();
    for (let r of items) {
      let card = node("div", "");
      card.className = "card";
      card.append(
        node("strong", r.title),
        node("p", `${r.channel || ""} · ${r.date || ""} · ${r.duration || ""}`),
      );
      let b = node("button", "XA401から直接再生");
      b.onclick = () => {
        playRecorded(r);
        location.hash = "playback";
      };
      card.append(b);
      $("recorded").append(card);
    }
    message(items.length + "件の録画番組を取得しました。");
  } catch (e) {
    message(e.message);
  }
};
refresh();
setInterval(refresh, 5000);
