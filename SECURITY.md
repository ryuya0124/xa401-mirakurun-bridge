# セキュリティ点検（2026-11-15）

「既知の問題なし」「完全に安全」という評価ではありません。現時点のPyPI依存は最新版ですが、未修正の既知リスクが1件あります。

## 依存関係

aiohttp 3.14.5、cryptography 50.0.2、python-dotenv 1.2.4などの最新版を確認し、uv.lockを更新。pydtcpipも確認時のHEAD 0114ed187d86902a879bcd775358cbb0ab917ba7へ固定しました。旧コミットとの差分はREADMEだけで、受信実装は同じです。ariblibは最新リリース0.2.0の公式wheelをハッシュ固定しています。CIもuv sync --frozenを使い、可変の依存解決を避けます。Actionsは確認した最新版のコミットSHAへ固定しました。

pip-auditは公開レジストリー依存を検査し、ecdsa 0.19.2にCVE-2024-23342 / GHSA-wj6h-64fc-37mpを検出しました。フィードの重複を除くと1件です。上流は修正版なしとしています。署名・鍵演算の実行時間に関する問題で、検証だけなら対象外ですが、pydtcpipは独自DTCP曲線の署名・鍵演算にも同ライブラリを利用するため無視しません。この曲線・本構成で実際に鍵を取り出せると実証したわけではありません。

- [上流のセキュリティ勧告](https://github.com/tlsfuzzer/python-ecdsa/security/advisories/GHSA-wj6h-64fc-37mp)
- [pydtcpipの独自曲線実装](https://github.com/tsukumijima/pydtcpip/blob/0114ed187d86902a879bcd775358cbb0ab917ba7/pydtcpip/dtcp/crypto.py)

Git/wheel直接参照のコード全体はPyPI脆弱性DBだけでは検査できないため、上流差分・ライセンス・使用箇所を別途確認しました。Python本体・OS・KonomiTV全体・Nginxの包括監査はこのBridge監査の対象外です。

## 接続境界の補強

- 互換APIはloopback限定。Hostを検査し、外部サイトからのOrigin / Sec-Fetch-Siteが付いた要求を拒否します。
- HTTPS中継も明示したallowed_hosts・LANサブネットで制限。allowed_hostsにはLAN IPと利用する独自ドメインを設定します。
- 持ち込まれたForwarded / X-Forwarded系ヘッダーを除去し、確認した接続元で付け直します。
- チャンク送信のアップロードも最大40 MiB・受信30秒。Host/Origin拒否、偽の転送元、通常本文とチャンク本文のサイズ制限をテストしました。
- 機器XMLはサイズ制限とdefusedxmlでDTD・外部実体を拒否。SOAPは読取り用Browseのみ。
- HTTP転送先は固定、DTCP受信先は確認済みXA401のホストに限定。通常HTTPリダイレクトには追従しません。
- 資格情報の値を例外・ログへ出さず、実秘密・映像・PCAPをGitへ含めません。

これらはLAN内ユーザーの本人認証を追加する機能ではありません。HTTPS中継をインターネットへ公開せず、信頼するLAN/Tailnet内で使います。既知ECDSAリスクを消す修正ではなく、定数時間の独自曲線バックエンドへの対応が残ります。暗号ライブラリの独自パッチで解消したとは報告しません。

## 継続検査

Testsワークフローは固定依存で3 OSを検査します。Dependency auditはロック変更・手動・毎週実行し、既知の1件も除外しません。そのため、修正版が出るまでは監査ワークフローが失敗として表示されます。テスト成功と脆弱性ゼロを混同しないための動作です。

```sh
python deploy/export_audit_requirements.py --output audit-requirements.txt
uv tool run --python 3.13 --no-python-downloads pip-audit --disable-pip --no-deps -r audit-requirements.txt
uv run --frozen --extra test python -m pytest -q
```

問題を報告する際は実秘密やパケットを添付せず、再現条件と機密情報を除いた結果を使用してください。
