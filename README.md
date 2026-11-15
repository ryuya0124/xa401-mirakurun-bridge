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
| `/api/programs` | 現在は空配列。EPG対応は未実装 |
| `/api/version` | アプリ名・バージョン |
| `/health` | 受信状態・認証完了回数・配信バイト数 |
| `/xa401/current-program-titles` | DMSで観測した現在の番組名。時刻未確定 |

- Mirakurun全APIの代替ではありません。録画予約・Move・ダビング・資格情報抽出・有効化は実装しません。
- 同時に異なるチャンネルを受信する数は保守的に1に制限します。同一チャンネルの複数クライアントは同じ受信を共有します。他チャンネルの要求は503です。XA401の複数同時配信能力は別途検証が必要です。
- 既定の`mobile1`はXA401で変換済みのH.264 720pです。元画質は`quality: original`で選択できますが、対応は実測を確認してください。
- 現在観測したmobile1と元画質のTSにはEITがありません。番組時刻やイベントIDを捏造せず、KonomiTVのEPGは空とします。映像受信とは別の制約です。
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
