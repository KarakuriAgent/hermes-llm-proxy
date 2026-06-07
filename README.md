# Hermes LLM Proxy

Hermesのprovider認証だけを流用して、OpenAI Responses APIへ直接リクエストを流すプロキシです。

このコンテナはHermesの `AIAgent` 会話ループを使いません。
SOUL、memory、tools、session保存、compactはすべて通りません。

## エンドポイント

- `GET /health`
- `GET /v1/models`
- `GET /v1/capabilities`
- `POST /v1/responses`

## 仕様

- 受け取ったResponses API bodyを基本的にそのままproviderへ渡します。
- `model` が未指定の場合だけ、Hermesの `config.yaml` のモデルを補います。
- HermesのOAuth/API key解決を使います。
- `stream: true` に対応します。
- Responses APIを直接持たないproviderではエラーになります。

## 主な環境変数

- `HERMES_SRC` default: `/opt/hermes`
  - Hermes source tree のパス。別パスにマウントする場合はこの値も変更してください。
- `HERMES_HOME` default: `/opt/data`

## 用途

Hermesの認証設定を再利用しつつ、Hermesのエージェント処理を通さずに
LLMを直接呼びたい場合に使います。
