# ReDiXiM Windowsの直接受信

Androidの受信・復号・送信を使わず、独立BridgeがXA401から直接受信する。
KonomiTVのインストールは不要。Windowsアプリは録画一覧・放送チャンネル・
時間指定再生をBridgeへ要求する。証明書・鍵はBridgeのPCにだけ置く。

- `GET /xa401/api/native/catalogue`：録画と放送の一覧、protocol=1
- `GET /xa401/api/native/recordings/{token}`：実録画の正確な総時間と同一originのstream URL
- `GET /xa401/api/native/channels/{service_id}`：放送の同一originのstream URL
- `/api/xa401/desktop-player`：WebView2に埋め込む映像面（独立したブラウザーUIではない）

別PCのBridgeは既存のLAN限定HTTPSフロントエンドから接続できる。
Windows設定にはその入口（例 `https://tv.example.test/`）を指定する。
HTTPフロントエンドも利用できるが、loopback APIを直接全ネットワークへ公開しない。
許可したLAN／Tailnetのクライアントだけに公開するプロキシを用意する。
公開インターネット向けの入口、CORSの緩和、TLS検証の無効化は追加していない。

HTTPは非暗号化。ReDiXiM側ではWebCodecsが使えるよう、その明示したoriginだけに
WebView2のsecure context例外を適用する。HTTPSの証明書検証は維持する。
アプリの再生URLとホストメッセージは同一origin／既知のパス・セッションに限定する。

2026-11-15、実際のWindows WebView2でloopback HTTPと別ホストのHTTPSから、
録画144件・放送134局の一覧、録画先頭・60秒・終盤の映像デコード、固定総時間、
一時停止／再開、Media Session録画名一致を検証した。Androidは使っていない。
全番組、長時間、全画面／小窓の目視は未検証。
