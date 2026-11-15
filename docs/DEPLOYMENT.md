# WindowsからUbuntu・macOSへ移す

このアプリとKonomiTVは別プロセス・別リポジトリです。橋渡しはloopbackのMirakurun互換HTTP APIだけで行います。

## Windowsで確認した構成

- Python 3.13.13（既存pyenv配下のPythonから専用venvを作成）
- 本アプリ専用`.venv`。調査リポジトリのモジュールはimportしない。
- pydtcpip: `e726fc548c01701f5949f8a93d47b45b038395df`
- KonomiTV: `2774e39e512b5f3dfd423fdfc8a7602e92e844f5`（0.15.0-dev）
- KonomiTVの配布済みclient/distを使用。Python依存は上流のuv.lockを使用。
- 第三者実行ファイルはKonomiTV v0.14.1のWindows版同梱ファイルを使用。この組み合わせでOriginal配信を検証した。
- XA401側は`original`、KonomiTV側も`original`。サーバーで映像の再圧縮を行わない。

安定版0.14.1にはライブOriginalのAPIがないため、今回の画質維持経路では使いません。NVEncCの720p変換は予備の確認だけです。

## 新しい端末で準備するもの

1. このリポジトリをcloneし、Python 3.13のvenvにインストールする。
2. 本人管理のローカル転送で資格情報を移す。GitHubやIssueに添付しない。
3. `config.json`を作り、XA401の現在のLOCATION・UDN、新しいPCのLANアドレス、資格情報のパスを指定する。
4. XA401の同一LANでアプリを起動し、`http://127.0.0.1:40772/health`を確認する。
5. KonomiTVを別途導入し、バックエンドをこのAPIに設定する。
6. 視聴画質に「Original (MPEG-2)」を選ぶ。解像度名の画質は再圧縮が入る。

UbuntuではKonomiTVのLinux版依存バイナリを使用します。Original配信はサーバーのハードウェアエンコーダーを起動しません。再圧縮モードも必要な場合だけ、Intel QSVなどを移行先で検証します。

macOSでは本アプリのコードは共通です。KonomiTV本体のネイティブ対応はWindows/Linuxなので、Mac側のKonomiTVはLinuxコンテナー等の別配置を検討してください。macOSの実機受信・Docker経由の通信は未検証です。コンテナーのNAT越えはDTCPのTTL/到達性に影響し得るため、単にポート公開するだけで動くとは扱いません。異なるホストへAPIを公開する構成には認証・通信保護が別途必要です。

## 起動補助

KonomiTVは先に上流の手順でインストールし、`server/`で`uv sync --frozen --no-dev`を完了させます。

```sh
python deploy/configure_konomitv.py --konomitv /path/to/KonomiTV --runtime /path/to/runtime
python deploy/start_stack.py --config config.json --konomitv /path/to/KonomiTV --uv /path/to/uv --runtime /path/to/runtime
```

`configure_konomitv.py`は新規configだけを作成します。既存設定を上書きしません。ローカルTLS証明書と、Originalを選択する補助HTMLを作ります。OSの信頼済み証明書は変更しません。

`start_stack.py`は自分で導入した2つのプロセスをバックグラウンド起動し、PIDとログをruntimeに残します。ポート使用中は既存サービスへ触らず終了します。サービス登録・OS自動起動の設定はしません。KonomiTV自体は`uv run --frozen --no-dev task serve`で起動します。

このPCだけで視聴するときは`http://127.0.0.77:7010/xa401-original.html`が入口です。127/8のloopbackを使用し、LANへ平文HTTPを公開しません。HTTPS入口は`https://localhost:7000/xa401-original.html`ですが、自己署名証明書のためブラウザーの警告があります。証明書警告を自動で無視する設定は行いません。

## スマホのブラウザーから接続する

受信・認証・復号を行うのはPCです。スマホではKonomiTVの画面を開くだけで、AndroidのDTCP受信アプリを経由しません。

1. `add_lan_certificate.py --runtime /path/to/runtime --ip PC_LAN_IP`でローカル証明書にLAN IPを追加し、自分で導入したKonomiTVを再起動する。
2. Windowsでファイアウォールに遮断される場合、管理者PowerShellで次を実行する。実行ファイル・ローカルIP・TCP 7000・指定したLANだけを許可する。

```powershell
./deploy/enable_lan_windows.ps1 -KonomiTVRoot C:/path/to/KonomiTV -LanAddress 192.168.1.10 -RemoteSubnets '192.168.1.0/24'
```

3. 同じLANのスマホで`https://PC_LAN_IP:7000/xa401-original.html`を開く。ローカル発行の証明書なので初回に信頼警告がある。OSの信頼済み証明書を自動で書き換える処理はない。
4. Originalを選択して視聴する。端末・ブラウザーのMPEG-2表示変換への対応は実機で確認する。

PCのIPが変わった場合、証明書とファイアウォールのローカルIPも確認してください。同名ルールは、このセットアップのGroupに属するものだけを更新します。Bridgeの40772や内部HTTPの7010をLANへ開放する必要はありません。ルーターのポート転送も不要です。

### WindowsのVPN経路競合がある場合

通常のAkebiの入口はシステムの応答経路を使います。今回のWindowsではTailscaleにLANと同じサブネットの優先経路があり、ルールを追加した後も別端末からの接続がタイムアウトしました。ファイアウォールだけが原因と確定したわけではありません。

システムのルートやVPNを変更しない代替として、Bridgeに**LANインターフェースを指定したHTTPSフロントエンド**を追加しました。設定例：

```json
"lan_https": {
  "listen": "192.168.1.10",
  "port": 7443,
  "allowed_subnets": ["192.168.1.0/24"],
  "certificate": "/private/runtime/tls/localhost.crt",
  "private_key": "/private/runtime/tls/localhost.key"
}
```

`listen`はBridgeの`source`と同じPC LANアドレスにします。Windowsでは待受ソケットにも`IP_UNICAST_IF`を指定します。元の映像・音声データを再圧縮せず、HTTPストリーミング・SSE・WebSocketをローカルのKonomiTV（固定の`127.0.0.77:7010`）へ中継します。任意のURLへ転送するオープンプロキシではありません。許可したプライベートサブネット以外はアプリ側でも拒否します。

この場合のスマホ入口は`https://PC_LAN_IP:7443/xa401-original.html`です。ファイアウォールは7443番と実際のPython実行ファイルへ限定します。Windowsのvenvランチャーと、ソケットを持つベースPythonが異なるため、`sys._base_executable`で確認した実行ファイルを指定してください。

```powershell
./deploy/enable_lan_windows.ps1 -ProgramPath C:/path/to/base/python.exe -Port 7443 -LanAddress 192.168.1.10 -RemoteSubnets '192.168.1.0/24'
```

アプリ側でHTTP本文・圧縮された本文・POST・WebSocketを変えずに中継するテストを追加しました。LANからの実到達や各スマホの表示対応は別途検証する項目です。

## 再圧縮されていないか比較する

両アプリが起動していて、他チャンネルを受信していないときに実行します。2本のローカルHTTP接続で同一チャンネルを共有受信します。実放送の映像・音声を各最大16 MiB保存するため、保存先はローカルの非公開フォルダーにしてください。

```sh
python deploy/verify_passthrough.py --service SERVICE_NUMBER --channel gr011 --out /private/verification --ffprobe /path/to/ffprobe
```

PAT/PMTやTSのPID・コンテナー全体が完全一致するという検査ではありません。映像と主音声の符号化データから、時間の重なった複数の大きなブロックがそのまま一致するかを検査します。ブラウザー表示や番組全体の保存成功とは別の指標です。
