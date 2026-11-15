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

## 再圧縮されていないか比較する

両アプリが起動していて、他チャンネルを受信していないときに実行します。2本のローカルHTTP接続で同一チャンネルを共有受信します。実放送の映像・音声を各最大16 MiB保存するため、保存先はローカルの非公開フォルダーにしてください。

```sh
python deploy/verify_passthrough.py --service SERVICE_NUMBER --channel gr011 --out /private/verification --ffprobe /path/to/ffprobe
```

PAT/PMTやTSのPID・コンテナー全体が完全一致するという検査ではありません。映像と主音声の符号化データから、時間の重なった複数の大きなブロックがそのまま一致するかを検査します。ブラウザー表示や番組全体の保存成功とは別の指標です。
