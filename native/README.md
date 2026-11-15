# Native Rust Bridge（移植中）

Python版は比較用・切り戻し用としてそのまま保持しています。KonomiTV本体は変更しません。

Rust単独でSSDP、機器UDN・model照合、ContentDirectory、原画質resource選択、Full AKE、証明書・SRM検証、RTT応答、PCP復号、188/192バイトTS正規化、同局共有受信とloopback APIを実装。Moveや資格情報発行は実装していません。資格情報は明示した外部ファイルから読みます。

```sh
cargo build --release --locked
target/release/xa401-bridge discover --source 192.168.1.10
target/release/xa401-bridge info --config /private/bridge.json
target/release/xa401-bridge list --config /private/bridge.json --recorded
target/release/xa401-bridge capture --config /private/bridge.json --service SERVICE_ID --seconds 15 --output sample.ts
target/release/xa401-bridge serve --config /private/bridge.json
```

設定はPython版と共通のキー（location、udn、source、credentials、state_dir、folders、quality、listen、port、max_streams）を使います。現段階のRustサーバーは**全APIの置換完了ではありません**。PC録画予約・管理画面・Konomi録画統合・LAN HTTPS frontendは移植作業中。比較時は別port・state_dirを使い、稼働中の録画を止めないでください。

暗号・AKE・SRMの処理と公開定数はMITの[pydtcpip](https://github.com/tsukumijima/pydtcpip/tree/0114ed187d86902a879bcd775358cbb0ab917ba7)を移植しています。権利表示は[licenses/pydtcpip-MIT.txt](../licenses/pydtcpip-MIT.txt)。暗号演算はOpenSSL、番組文字列はMITのarib crate。実資格情報をソースへ埋め込まないでください。

2026-11-16実機：Intel N100 DebianでRustバイナリー単独からFull AKE・放送TS取得に成功。12秒、24,636,648バイト、188バイトTS、MPEG-2 1920×1080/AAC、FFmpeg 3秒デコードexit 0。開始途中のGOP由来の警告あり。これは全録画・全チャンネル・長時間安定性や従来API全互換の検証ではありません。
