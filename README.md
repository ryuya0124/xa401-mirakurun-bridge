# XA401 Mirakurun Bridge

J:COM LINK XA401のライブ配信をPCで受信し、Mirakurun互換APIの一部として提供する独立アプリです。KonomiTVを変更せず接続できます。メーカーやDTLAによる認証製品ではありません。

```
XA401 -- DLNA / DTCP-IP --> このアプリ -- HTTP MPEG-TS --> KonomiTV --> ブラウザー
```

## インストール

Python 3.13を用意し、このリポジトリで実行します。Pythonの管理方法は既存環境に合わせてください。

```sh
python -m venv .venv
# Windows
.venv/Scripts/python -m pip install -e .
# Ubuntu / macOS
.venv/bin/python -m pip install -e .
```

`config.example.json`を`config.json`にコピーし、XA401のDMS LOCATION・UDN、同一LAN上の送信元IP、資格情報ファイルの場所を設定します。資格情報は同梱・発行しません。明示的に指定した証明書と対応秘密鍵のみを読みます。値はログへ出しません。

```sh
xa401-bridge discover --source 192.168.1.10
xa401-bridge serve --config config.json
```

仮想環境を有効にしない場合、`xa401-bridge`は`.venv/Scripts/xa401-bridge.exe`（Windows）、`.venv/bin/xa401-bridge`（Ubuntu / macOS）にあります。

SSDPが届かない機器では現在のLOCATIONをseedに指定します。seedは固定IPを保証するものではなく、起動・更新・選局ごとにUDNとXA401の機種名を検証します。違う機器から配信を受けません。送信元IPが変わった場合も設定を更新してください。

## KonomiTVとの接続

KonomiTVの`config.yaml`で次を設定します。両アプリは同じホストで動かしてください。

```yaml
general:
    backend: 'Mirakurun'
    mirakurun_url: 'http://127.0.0.1:40772/'
```

エンコーダーはKonomiTV側で選びます。WindowsのNVIDIA GPUならNVEncC、Intel GPUならQSVEncCなど、移行先で実際に使用できるものへ変更してください。このアプリ自体はGPUを必要としません。既存サーバーへの設定変更・再起動はその管理手順に従ってください。

## APIと制約

| URL | 内容 |
| --- | --- |
| `/api/services` | 現在のDMSライブチャンネル一覧 |
| `/api/services/{networkId*100000+serviceId}/stream` | AKE・復号後の188バイトTS |
| `/api/tuners` | 1チューナーとしての利用状態 |
| `/api/programs` | 視聴・任意の空き時間収集でSITから実測した現在番組の名称・開始時刻・放送時間・説明・ジャンル |
| `/api/version` | アプリ名・バージョン |
| `/health` | 受信状態・認証完了回数・配信バイト数 |
| `/xa401/current-program-titles` | DMSで観測した現在の番組名。時刻未確定 |

- Mirakurun全APIの代替ではありません。PCの日時指定録画・予約とXA401録画の直接再生に対応します。STBへの録画予約・削除・Move・ダビング・資格情報抽出・有効化は実装しません。[録画とKonomiTV標準UIへの連携](docs/RECORDING.md)。
- 異なる原画質2局の同時受信を実機確認しました。設定例の`max_streams`は2、省略した旧設定は1。同一局の複数視聴は受信を共有し、1局視聴中はもう1枠で番組情報を収集できます。2局視聴中の第3局要求は503です。物理チューナー数とは別です。[仕様と実測](docs/CONCURRENCY.md)。
- 既定の`mobile1`はXA401で変換済みのH.264 720pです。元画質は`quality: original`で選択できますが、対応は実測を確認してください。
- XA401の部分TSにはEITがなくても、SITに現在番組の情報が含まれます。CRC・NID/TSID/SIDを照合し、実際の開始時刻・放送時間・イベントIDをKonomiTVへ渡します。`epg_collection.enabled`で空き時間の各局収集を有効にできます。視聴要求が来たら収集を中断し、視聴を優先します。未来・週間の番組表は取得しません。[収集の設定と制約](docs/EPG.md)。KonomiTVの更新間隔は0.5分を推奨し、取得後の初回反映に最大30秒程度かかる場合があります。
- TSの到着時刻プレフィックスを外します。CCIや符号化映像を書き換えません。外側の暗号化状態と、内側のコピー制御・出力の許諾を混同しないでください。
- APIには認証がないためloopback専用です。インターネットに公開しないでください。資格情報・設定・生通信記録・映像はGit管理の対象外です。
- スマホ向けには、KonomiTVの画面だけを中継する任意のLAN HTTPS入口を有効にできます。Mirakurun APIのLAN公開ではありません。設定とファイアウォールの範囲は[導入手順](docs/DEPLOYMENT.md)を参照してください。
- Ubuntu / macOSへの移行では同じコードをインストールし、設定のIP・パスだけ更新します。資格情報は本人管理のローカル経路で移し、GitHubへ置かないでください。macOS実機の動作確認は未実施です。

## テスト

```sh
python -m pip install -e '.[test]'
python -m pytest -q
```

人工TSのテストと実機での配信・デコード検証は別々に記録します。実測結果は`docs/`に追加します。

## ライセンス

本アプリはMIT。認証・復号は[pydtcpip](https://github.com/tsukumijima/pydtcpip)（MIT、固定コミット使用）を利用します。KonomiTV本体・バイナリ・資格情報は配布しません。各技術の特許・商標・コンテンツの権利を許諾するものではありません。

SITのARIB記述子・文字列はariblib 0.2.0（MIT、公開wheelを固定）で読み取ります。[ライセンス通知](licenses/ariblib-MIT.txt)を同梱します。TLSを独自ドメインで使う構成は[非公開HTTPSの設定](docs/PRIVATE_HTTPS.md)を参照してください。

## コード構成とセキュリティ

WindowsのWinUI番組表向けに、`GET /xa401/api/native/guide`で観測済みSITの現在・直近の番組を配信します。
放送局・開始時刻・長さ・番組名・説明を返し、未取得局はprogram=null、未来予定の取得可否はfalseです。
閲覧による追加受信は行わず、既存キャッシュだけを読みます。保護境界・HTTP/HTTPS入口は既存のnative APIと同じです。

探索・XMLはdevice.py、DTCP受信はreceiver.py、受信の共有単位はsession.py、2枠管理と互換APIはserver.py、SIT解析とキャッシュはepg.py、収集はcollector.py、HTTPS中継はlan_proxy.py、Host/Origin検査はhttp_security.pyに分離しています。PC録画はrecording.py／ts_file.py、管理APIはrecording_api.py、XA401直接再生はrecorded_playback.py、標準録画UIへの変換はkonomi_recordings.py、ブラウザー側の時間ソースはweb/native-player.mjsです。配備用コードはdeploy/、検査はtests/です。

依存はuv.lockで固定し、CIでも同じロックを使用します。最新版でも未修正のecdsaリスク1件が残ります。[監査結果と残る制約](SECURITY.md)を確認してください。
