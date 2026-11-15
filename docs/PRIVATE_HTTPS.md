# 独自ドメインでLAN・Tailscale専用のHTTPSを使う

実機構成の入口は `https://tv-home.ryuya-dev.net/xa401-original.html`。DNSは内部のAdGuardへだけ設定し、公開DNSレコード・Cloudflare Tunnelのルートは追加していません。LANとTailnetからOS標準の信頼ストアによる証明書検証付きで200、OriginalのTS受信も確認しました。

**この環境のDNSの親はMacです。** `adguardhome-sync`が10分ごとにMacからDebianへ同期します。初回にDebianだけへ追加したレコードは次の同期で消え、スマホのNXDOMAINを再現しました。Macの親AdGuardへ公式の`/control/rewrite/add` APIでA/AAAAを追加し、Debianにも同じ2件を反映しました。13:10 JSTの自動同期後も両側に残り、同期のエラー0を確認。ほかのrewrite・同期スケジュールは変更していません。再配置時も、同期先だけを変更せず先に同期元を確認してください。

## 実際の構成

```
LAN / Tailnetのブラウザー
  → Debianの専用Nginx（正規の *.ryuya-dev.net 証明書）
  → WindowsのLAN固定HTTPS入口
  → KonomiTV / XA401 Bridge
```

- 利用者が既に運用しているワイルドカード証明書を再利用。確認時の期限は2027-01-19。Cloudflare APIトークンを読取り・コピーせず、証明書の新規発行も行っていません。
- Debianの既存IP `192.168.3.254:443` とTailnet IPv6 `[fd7a:115c:a1e0::36:1645]:443` だけで待受。既存の別アプリが使用する443番のIPは変更しません。
- 内部DNSのAは`192.168.3.254`、AAAAはDebianのTailnet IPv6。Tailscaleの既存DNS `10.7.0.53` と内部DNS `192.168.3.254`、Macの5354番から生UDPでA/AAAAを確認しました。**LANルーターのDNS `192.168.3.1` は内部名を解決しません。** 初回のWindows上の明示DNS先検査はTailscaleのDNS介入があり、後のMac/Debianの生UDP検査で訂正しました。LANでも内部DNSを使用する端末設定が必要です。
- IPv4 LAN・Tailnet IPv4/IPv6以外はNginxで拒否。Cloudflare HTTP経由の`CF-Ray`ヘッダー付き要求も403を返すことを確認しました。Cloudflareの公開HTTP入口を作る設定ではありません。
- NginxからWindowsへもHTTPSで接続。ローカルCAを明示的に信頼し、証明書の`localhost`名を検証するため、検証無効化は使用しません。
- 公開用秘密鍵はDebianに残ります。Nginxには対象ドメインのlive/archiveだけを読み取り専用マウントし、ACMEアカウントやCloudflare認証ファイルはマウントしません。
- 既存の証明書更新処理は変更しません。専用コンテナー内のwatcherが公開証明書チェーンの変更を5分ごとに検出し、設定検査に通った場合だけNginxを再読み込みします。実際の次回更新は未到来で、強制更新の試験はしていません。

実映像の取得・DTCPの認証と復号はWindowsで行います。DebianのNginxは符号化映像を再圧縮せずに中継します。

## 別環境で再現するとき

1. 自分のドメインに有効な証明書を用意する。既存証明書を使うか、必要ならDNS-01等を別途準備する。鍵をGitやIssueへ置かない。
2. `private_https.nginx.conf.example`のホスト名・待受IP・Windowsバックエンド・証明書名・許可CIDRを、自分の環境へ置換して保存する。既に使用中のIP/ポートへ割り込まない。
3. `private-https.env.example`を`private-https.env`へコピーし、実際のファイルパスを設定する。
4. Dockerの設定を検査してから、この専用サービスだけを起動する。

```sh
docker compose --env-file deploy/private-https.env -f deploy/private_https.compose.yaml config
docker compose --env-file deploy/private-https.env -f deploy/private_https.compose.yaml run --rm --entrypoint nginx private-https -t
docker compose --env-file deploy/private-https.env -f deploy/private_https.compose.yaml up -d
```

既に同名の手動作成コンテナーがある場合、Composeはそのコンテナーを勝手に管理対象にしません。所有者・ラベル・マウントを確認し、既存サービスの管理手順に合わせてください。今回の稼働コンテナーは`xa401-private-https`、管理ラベルは`com.ryuya.xa401.private_https=1`です。

## 内部DNSの追加

`prepare_private_dns.py`は、明示したAdGuardHome.yamlへ対象ホストのA/AAAAの2エントリーだけを加えるオフライン設定用の補助コードです。`ruamel.yaml`が必要です。元の設定をバックアップし、候補を書き出す段階と実際に適用する段階を分けます。認証や上流DNSなど他設定が変わらないことを検査します。**同期構成では必ず親側を変更してください。** 通常稼働中の追加は親AdGuardの管理画面または公式rewrite APIを使い、同期間隔の経過後に子側でも残ることを確認します。

```sh
python prepare_private_dns.py prepare --config /config/AdGuardHome.yaml --hostname tv.example.net --ipv4 192.168.1.250 --ipv6 fd7a:115c:a1e0::REPLACE
# 対象AdGuardで --check-config -c 候補ファイル を実行してから、対象インスタンスを停止する。
python prepare_private_dns.py activate --config /config/AdGuardHome.yaml
# 対象インスタンスを起動し、通常のDNS問い合わせでA/AAAAと既存の名前解決を検査する。
```

候補準備後に元の設定が変わっていれば適用を拒否します。問題があれば`restore`でこの操作前のバックアップへ戻せます。今回の設定は候補のAdGuard構文検査に合格し、再起動後に内部DNS・HTTPS・映像配信を確認しました。

## 証明書と到達性を分けて調べる

- DNSが見つからない：親Macと子Debianの両方のrewriteを確認する。内部DNSやTailnet DNSを使っているかも確認する。公開DNSにはこのホストを登録していません。修正後も端末に失敗のキャッシュが残る場合は、少し待ってからブラウザーの再起動やWi-Fiの再接続で再確認します。
- 証明書警告：IP直打ちの旧入口ではなく、新しいドメイン名を使う。正規証明書の対象はホスト名です。
- 502：WindowsのKonomiTV/Bridgeや、Windows側HTTPSを確認する。
- 映像は出るが番組情報が遅い：SITを受け取ってからKonomiTVが取り込むまで、更新間隔分の待ちがある。現在番組だけを収集し、未視聴チャンネルの週間EPGは生成しない。

OSのVPN経路やTailscaleの全体設定は変更していません。外部公開用のポート転送やCloudflare Tunnelは不要です。
