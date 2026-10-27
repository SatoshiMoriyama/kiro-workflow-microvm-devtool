# Lambda MicroVMs 調査メモ

調査日: 2026-10-27
テーマ: Lambda MicroVMs 上で Kiro を動かし、git clone → 開発 → push/PR → 通知までを回せるか

## 1. Lambda MicroVMs とは

- ユーザー/AI が生成した信頼できないコードを、隔離されたステートフルな実行環境で安全に動かすための新しいサーバーレスコンピュートプリミティブ。
- 基盤は Firecracker（Lambda 関数の実行を支えてきたのと同じ軽量仮想化技術）。
- 従来 Lambda 関数（Event/Web Functions）とは別の capability。
- ゲスト OS は Amazon Linux 2023。アプリは「Firecracker microVM の中で動くコンテナ」。ローカルで同じ環境を再現できる。

### 解決する課題
AI コーディングアシスタント、インタラクティブなコード実行環境、データ分析、脆弱性スキャナ、ゲームサーバーなど「開発者が書いていないコードをエンドユーザーごとの専用環境で安全に実行したい」ケース。従来の選択肢のトレードオフを埋める:
- VM: 隔離は強いが起動に数分
- コンテナ: 起動は速いがカーネル共有でハードニングが必要
- FaaS(通常の Lambda): イベント駆動向けで長時間ステートフルセッションに不向き

### 主な特徴
1. スナップショットベースの高速起動（Dockerfile から事前初期化した Firecracker スナップショット=MicroVM イメージ。サブ秒起動）
2. 専用 HTTPS エンドポイント（個別ポート。HTTPS/WebSocket/gRPC/SSE/HTTP2）
3. ライフサイクル制御とステート保持（最長 8 時間。suspend でメモリ・ディスク状態保持、resume で復帰）
4. 垂直・水平スケーリング（既定 2GB/1vCPU、最大 8GB/4vCPU、CPU:メモリ=2:1。垂直自動 4x、水平は1分で数百起動。per-MicroVM 上限 16 vCPU / 32 GB）
5. ネットワーク（既定でアウトバウンド・インターネット接続可。プライベート VPC は Lambda Network Connector(LNC)）

### 隔離モデル（Lambda FAQ）
- Event Functions: リクエストごとに専用環境で隔離
- Web Functions: 1 環境で複数リクエスト同時処理（環境は他から VM 隔離）
- MicroVMs: 同じ Firecracker 隔離 + 顧客がライフサイクル制御 + セッションまたぎで状態保持

### 2リソース/2ロールモデル
- `MicrovmImage`: `{S3 zip with Dockerfile} + baseImageArn` から作る versioned 成果物。
- `Microvm`: イメージ版から `RunMicrovm` で起動する実行インスタンス。
- `buildRoleArn`: イメージビルド時に使う（S3 read, CloudWatch logs, 任意で ECR）。
- `executionRoleArn`: 実行時に MicroVM が assume するロール。

## 2. 用途の確定: MicroVM 内で Kiro を回す

方向性: 「MicroVM の中で Kiro を立ち上げて git clone → 開発 → push or PR 作成まで回す」

公式が主要ユースケースに「interactive development environments」「AI code execution sandboxes」を明記しており、素直にハマる。

### 全体フロー
```
[ランチャー（管理アプリ / Lambda関数）]
  1. run-microvm でMicroVM起動（executionRoleArn + SHELL_INGRESS）
  2. CreateMicrovmShellAuthToken でシェルトークン発行
       │ WebSocket (port 8022) で接続
[MicroVM ゲスト内（Amazon Linux 2023 コンテナ）]
  3. execution role の一時credentialでSecrets Managerから
     git/Kiro の認証情報を取得
  4. git clone → Kiroで開発 → commit → push / PR作成
```

## 3. 認証情報の渡し方（設計の核心）

- ゲスト内には execution role の **短命 credential が自動で入る**（"Guest uses short-lived credentials from MicroVmExecutionRoleArn"）。長期 AWS キーの埋め込み不要。ゲスト内で `aws secretsmanager get-secret-value` が直接叩ける。
- git/Kiro の認証情報は **値ではなく ARN 参照で渡す**のが定石（AWS 自己ホスト型エージェントサンドボックスのブログ推奨）。
  - Secrets Manager に git トークン（fine-grained PAT / GitHub App installation token）と Kiro 認証情報を格納
  - `run-microvm --run-hook-payload`（MicroVM ごとに一意、最大 16KB）に **シークレットの ARN だけ**渡す
  - ゲストの `/run` フックまたは起動スクリプトが execution role で `GetSecretValue`
  - execution role は対象シークレット ARN・リージョン・アカウントに最小権限スコープ

### スナップショットの落とし穴（必ず踏む）
- MicroVM は memory+disk スナップショットから起動。**イメージビルド時に認証情報を焼くと全セッションで共有**される。
- 対策: 認証情報はビルド時でなく run/resume 時に取得。git トークンは短命（GitHub App installation token は1時間失効が理想）。resume のたびに取り直す（`/resume` フック）。CSPRNG の再シードと secret ローテーションも推奨。

### git push / PR 作成の具体手段
- push: `https://x-access-token:<token>@github.com/org/repo.git` 形式、または GIT_ASKPASS / credential helper で注入
- PR 作成: ゲストに `gh` CLI 同梱、`GH_TOKEN` 注入 → `gh pr create`
- egress は既定でインターネット可（社内 Enterprise なら LNC + VPC egress）

### Kiro をゲストで動かす点
- MicroVM イメージ(Dockerfile)に Kiro CLI, git, gh, 言語ランタイムを同梱
- Kiro 自体の認証も Secrets Manager 経由で run 時注入
- エージェントにシェルを触らせるなら `SHELL_INGRESS` + `CreateMicrovmShellAuthToken`（PTY over WebSocket, port 8022）。トークンは最大 60 分なので長時間は更新が必要

## 4. run-microvm と主要 API（確認済み）

### 主要パラメータ
| パラメータ | 説明 |
|---|---|
| `--image-identifier` | (必須) MicroVM イメージの ARN |
| `--image-version` | 版。既定は最新 active |
| `--execution-role-arn` | 実行時に MicroVM が使う IAM ロール |
| `--idle-policy` | 自動 suspend/resume。`autoResumeEnabled` / `maxIdleDurationSeconds` / `suspendedDurationSeconds` |
| `--maximum-duration-in-seconds` | 最大寿命（running+suspended 合算）。1〜28,800 秒(8h) |
| `--run-hook-payload` | `/run` フックに渡す文字列(最大16KB)。MicroVM ごとに一意 |
| `--logging` | CloudWatch ログ設定 |
| `--ingress-network-connectors` | インバウンド接続。無効化は `NO_INGRESS`。シェルは `SHELL_INGRESS` |
| `--egress-network-connectors` | アウトバウンド（`INTERNET_EGRESS` / VPC） |

### 接続
- 全リクエストに JWE 認証トークン必須（`create-microvm-auth-token`、最大 60 分、allowedPorts でポート制限）。ヘッダ `X-aws-proxy-auth`、ポート指定は `X-aws-proxy-port`（既定 8080）。
- プロトコル: HTTP/2, WebSocket, gRPC, SSE。

### ライフサイクルフック（パス: `/aws/lambda-microvms/runtime/v1/<hook>`）
ビルド時(microvmImageHooks):
- `ready`: アプリ起動完了のシグナル（200 でスナップショット取得可）。timeout 1–3600s
- `validate`: スナップショットからの起動後の検証 + prefetch 用サンプリング。timeout 1–3600s

実行時(microvmHooks, timeout 1–60s の高速通知用):
- `run`: スナップショットから起動後1回。これが 200 を返すまで外部トラフィックは届かない
- `resume`: SUSPENDED→RUNNING 後。credential 再取得・接続再確立
- `suspend`: RUNNING→SUSPENDED 前。フラッシュ・切断
- `terminate`: 最終終了直前のみ（suspend では走らない）。**外部システムへの通知用途を公式が明記**

### CLI 例
```bash
# イメージ作成
aws lambda-microvms create-microvm-image \
  --name my-image \
  --base-image-arn arn:aws:lambda:<region>:aws:microvm-image:al2023-1 \
  --build-role-arn arn:aws:iam::<acct>:role/MicroVMBuildRole \
  --code-artifact '{"uri":"s3://<bucket>/<key>.zip"}'

# 実行
aws lambda-microvms run-microvm \
  --image-identifier arn:aws:lambda:<region>:<acct>:microvm-image:my-image \
  --image-version 1.0 \
  --execution-role-arn arn:aws:iam::<acct>:role/MicroVMExecutionRole \
  --idle-policy '{"maxIdleDurationSeconds":900,"suspendedDurationSeconds":300,"autoResumeEnabled":true}'

# シェルトークン + WebSocket 接続
TOKEN=$(aws lambda-microvms create-microvm-shell-auth-token \
  --microvm-identifier microvm-... --expiration-in-minutes 15 \
  --query 'authToken."X-aws-proxy-auth"' --output text)
websocat "wss://<endpoint>/shell" \
  -H "Sec-WebSocket-Protocol: lambda-microvms.authentication.${TOKEN}, lambda-microvms, lambda-microvms.port.8022"

# ライフサイクル
aws lambda-microvms suspend-microvm   --microvm-identifier microvm-...
aws lambda-microvms resume-microvm    --microvm-identifier microvm-...
aws lambda-microvms terminate-microvm --microvm-identifier microvm-...
```

### 既知の制約
- イメージは単一サイズ（サイズごとに別イメージ）
- イメージ版は MicroVM 未起動でもストレージ課金。`delete-microvm-image-version` で掃除
- suspend→resume でネットワークコネクタは切替不可（LNC は run 時バインド）
- **ゲスト内から自己 suspend 不可**（`SuspendMicrovm` は外部 API のみ）
- 認証トークン最大 TTL 60 分
- 実行時フックは高速通知用（重い init は不可）

### run エラー
`ServiceQuotaExceededException` / `ResourceNotFoundException` / `ValidationException` / `ThrottlingException`。transient は指数バックオフ+ジッタでリトライ。

## 5. 作業完了の通知（MicroVM 側）

2 系統で設計。

### A. ゲスト内アプリが能動通知（本命）
egress 既定可なのでゲストから直接外へ。
- Slack/Teams/Discord Webhook を叩く（push/PR 完了後に PR URL 付き）
- SNS/EventBridge に publish（execution role に権限、後段ファンアウト）
- GitHub の仕組みに乗せる（`gh pr create` で PR ができれば GitHub 側通知が自然発火）
- ランチャーのコールバック URL に POST（run-hook-payload で署名付き URL を注入）
- ポイント: 構造化 JSON で status/PR URL/変更ファイル/ログ要約を送る。通知本文にシークレットを混ぜない。

### B. ライフサイクルを AWS 側で捕捉（受動通知）
- `/terminate` フック（最終終了のみ。公式が外部通知用途を明記。timeout 1–60s）
- CloudTrail に run/suspend/resume/terminate API が記録 → EventBridge ルールで拾って SNS/Chatbot
- CloudWatch Logs メトリクスフィルタ+アラーム（完了ログ欠落検知）
- idle policy の auto-suspend/terminate も CloudTrail で検知

### 役割分担表
| 通知したいこと | 手段 | 発火点 |
|---|---|---|
| 開発成功(PR作成完了) | ゲスト→Slack Webhook + SNS | push/PR 成功直後 |
| 作業の成否サマリ | ゲスト→コールバックURL(ランチャー) | 完了処理の最後 |
| 終了イベント(正常/中断の最終解放) | `/terminate` フック→Slack/SNS | terminate 時のみ |
| 誰がいつ run/terminate したか(監査) | CloudTrail→EventBridge→SNS | API 呼び出し時 |
| 放置・異常(完了ログ欠落) | CloudWatch Logs フィルタ+アラーム | ログ欠落検知 |

- `/suspend` では `/terminate` は走らないので「中断された」と「本当に終わった」を区別できる。
- ゲストは自己 suspend/terminate できないので、完了コールバック → ランチャーが `terminate-microvm` を呼ぶ流れにする。

### EventBridge イベントパターン（CloudTrail 経由, 想定）
```json
{
  "source": ["aws.lambda-microvms"],
  "detail-type": ["AWS API Call via CloudTrail"],
  "detail": {
    "eventSource": ["lambda-microvms.amazonaws.com"],
    "eventName": ["RunMicrovm", "TerminateMicrovm", "SuspendMicrovm"]
  }
}
```
注: MicroVMs 専用ネイティブイベントの有無は Monitoring 章で要確認。確実性重視なら CloudTrail 証跡有効化 + 上記パターン。

## 6. ローカルで動いている Kiro が通知を受け取れるか

結論: Kiro CLI に「外部 HTTP push を待ち受けてセッションに割り込む」ネイティブ機能はドキュメント上なし。ただし既存機能で実質的に受け取れる。手段は3つ。

### 手段1: Kiro の Hooks でポーリング（一番素直）
- Kiro の Hooks System は agentSpawn / userPromptSubmit / preToolUse / postToolUse / stop で任意コマンド実行可。
- `userPromptSubmit`（または `stop`）フックで、MicroVM 完了を SQS/S3/コールバックキューから取り出し STDOUT に出す → exit 0 の STDOUT は会話コンテキストに追加される。
- 設定例（`~/.kiro/agents/<agent>.json` あるいは default agent）:
```json
{
  "name": "default",
  "hooks": {
    "userPromptSubmit": [
      { "command": "~/.kiro/hooks/check-microvm-done.sh" }
    ]
  }
}
```
```bash
#!/bin/bash
# ~/.kiro/hooks/check-microvm-done.sh
MSG=$(aws sqs receive-message --queue-url "$MICROVM_RESULT_QUEUE" \
      --max-number-of-messages 1 --wait-time-seconds 0 \
      --query 'Messages[0].Body' --output text 2>/dev/null)
if [ -n "$MSG" ] && [ "$MSG" != "None" ]; then
  echo "【MicroVM作業完了通知】$MSG"
fi
exit 0
```
- 制約: 発火はプロンプト送信時やターン境界に限る。放置中に勝手にポップするバックグラウンドタイマーはドキュメント上なし。

### 手段2: ACP 経由でプログラムから割り込む（一番 push に近い）
- `kiro-cli acp` で JSON-RPC over stdio。`session/prompt` で外部からプロンプト注入、`session/load` で既存セッション読込。
- ローカルに受信ブリッジ（HTTP/WebSocket）を立て、MicroVM 完了通知が来たら `session/prompt` で「作業完了。PR: …」を注入。
```
MicroVM完了 → ローカルの受信ブリッジ → kiro-cli acp に session/prompt 投入 → Kiro が反応
```
- 対話 CLI そのものへの割り込みではなく、ACP セッションを張る構成が前提。受信サーバ部分は自作。

### 手段3: OS のターミナル通知（気づくだけ）
- `chat.enableNotifications` で応答完了・承認要求・入力要求時に端末通知（bel / osc9、未フォーカス時のみ、Windows は focus tracking 不可で発火しない）。
- ただし Kiro 自身のイベント対象で MicroVM 外部イベントは対象外。手段1 でターン内に取り込めば間接的に気づける程度。

### 推奨
- 手軽さ重視 → 手段1（Hooks ポーリング）。SQS/S3/コールバックキューに積み、userPromptSubmit/stop で引き出す。
- 自動で割り込ませたい → 手段2（ACP ブリッジ）。受信プロセス自作。放置中でも反応可。

## 7. 未確認事項（要追加調査）
- MicroVM ゲスト内への IAM credential 伝播の詳細メカニズム（Security and permissions 章）
- MicroVMs 専用ネイティブ EventBridge イベント（ステート変化直接発行）の有無（Monitoring 章）
- 料金の実額（AWS Lambda Pricing）
- Kiro CLI の自動ポーリング（タイマー）機構はドキュメント上なし

## 参考 URL
- Lambda MicroVMs ガイド: https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html
- Running and using MicroVMs: https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html
- 発表ブログ(Compute): https://aws.amazon.com/blogs/compute/announcing-lambda-microvms-serverless-compute-environments-with-vm-level-isolation-and-near-instant-startup/
- 発表ブログ(News): https://aws.amazon.com/blogs/aws/run-isolated-sandboxes-with-full-lifecycle-control-aws-lambda-introduces-microvms/
- 自己ホスト型エージェントサンドボックス(Security): https://aws.amazon.com/blogs/compute/running-self-hosted-ai-agent-sandboxes-with-aws-lambda-microvms/
- Lambda FAQ: https://aws.amazon.com/lambda/faqs/
