#!/usr/bin/env bash
# MicroVM を起動し、/run フックが S3 に吐いた結果を回収して終了する。
#
# S3 レイアウト(入力 key / 出力 prefix はいずれも <requestId> を先頭に置く):
#   入力 : s3://$RESULT_BUCKET/<requestId>/inputs/prompt.md  (UTF-8。本スクリプトが put)
#   出力 : s3://$RESULT_BUCKET/<requestId>/runs/             (status.json, report.json,
#          report.md, kiro-stdout.txt, artifacts など。hook_server が put)
# payload は prompt 本文ではなく promptKey(=<requestId>/inputs/prompt.md)を運ぶ。
# これは GitHub issue #3(runHookPayload は最大 4096 バイト。日本語 prompt は JSON 内で
# 約3倍に膨らむ)への対処で、prompt を S3 経由にして payload を小さく保つ。
# 直接 payload に prompt を積む旧方式も hook_server 側は受理する(後方互換)。
# prefix が payload に無い場合、hook_server は <microvmId>/runs/ にフォールバックする。
#
# 前提: cdk deploy 済み + scripts/package-image.sh でイメージビルド済み。
# 使い方(AWS_PROFILE は必須。既定のフォールバックは無い):
#   AWS_PROFILE=<profile> ./scripts/run.sh
#   AWS_PROFILE=<profile> PROMPT="東京の天気を調べて weather.html を作って" ./scripts/run.sh
#   AWS_PROFILE=<profile> ./scripts/run.sh "東京の天気を調べて weather.html を作って"
#   # GitHub フロー(clone→ブランチ作成→作業→commit→push→作業報告):
#   AWS_PROFILE=<profile> REPO_URL="https://github.com/owner/repo.git" \
#     PROMPT="READMEに概要を追記して" ./scripts/run.sh
#   AWS_PROFILE=<profile> ./scripts/run.sh "READMEに概要を追記して" "https://github.com/owner/repo.git"
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
# AWS プロファイルは必須。フォールバック既定は置かない(公開前提、環境依存のプロファイル名を
# 決め打ちしない)。AWS_PROFILE 未指定なら明示エラーで止める。
PROFILE="${AWS_PROFILE:-}"
[[ -n "$PROFILE" ]] || { echo "ERROR: AWS_PROFILE が未指定。例: AWS_PROFILE=<profile> pnpm run-microvm" >&2; exit 1; }
STACK="${STACK_NAME:-KiroMicrovmBase}"
IMAGE_NAME="${MICROVM_IMAGE_NAME:-kiro-microvm}"

# prompt は PROMPT 環境変数 or 第1引数で受け、両方無ければデフォルト(hook_server と同一)。
DEFAULT_PROMPT="札幌の天気を調べて weather.html を作って"
PROMPT="${PROMPT:-${1:-$DEFAULT_PROMPT}}"

# repoUrl / baseBranch は動的指示(GitHub フロー)。
#   repoUrl: REPO_URL 環境変数 or 第2引数。空なら従来どおり clone なしで prompt 実行。
#   baseBranch: BASE_BRANCH 環境変数、既定 main。
REPO_URL="${REPO_URL:-${2:-}}"
BASE_BRANCH="${BASE_BRANCH:-main}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_DIR_BASE="$ROOT/results"

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

get_output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
RESULT_BUCKET="$(get_output ResultBucketName)"
EXEC_ROLE_ARN="$(get_output ExecutionRoleArn)"
IMAGE_ARN="$(aws lambda-microvms list-microvm-images \
  --query "items[?name=='$IMAGE_NAME'].imageArn | [0]" --output text)"

[[ -n "$RESULT_BUCKET" && "$RESULT_BUCKET" != "None" ]] || { echo "ERROR: ResultBucketName 未解決(cdk deploy 済みか)" >&2; exit 1; }
[[ -n "$IMAGE_ARN" && "$IMAGE_ARN" != "None" ]] || { echo "ERROR: イメージ '$IMAGE_NAME' が無い(ビルド完了か)" >&2; exit 1; }

REQUEST_ID="${REQUEST_ID:-req-$(date +%Y%m%d-%H%M%S)-$$}"
PREFIX="${REQUEST_ID}/runs/"
PROMPT_KEY="${REQUEST_ID}/inputs/prompt.md"
INTERNET_EGRESS_ARN="arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"

# prompt 本文を UTF-8 で S3(既存の result バケット)へ put する。payload には本文では
# なくこの key(promptKey)を積むことで、runHookPayload を 4096 バイト以内に保つ。
# body は一時ファイル経由で渡し、put 後に削除する。tagging で入力オブジェクトを識別する。
PROMPT_TMP="$(mktemp)"
printf '%s' "$PROMPT" > "$PROMPT_TMP"
aws s3api put-object \
  --bucket "$RESULT_BUCKET" \
  --key "$PROMPT_KEY" \
  --body "$PROMPT_TMP" \
  --content-type "text/markdown; charset=utf-8" \
  --tagging "kind=input" >/dev/null
rm -f "$PROMPT_TMP"

# payload に promptKey / baseBranch / prefix / requestId を積む。repoUrl が空なら
# repoUrl キー自体を省き、hook_server 側は従来どおり clone なしで prompt を実行する。
RUN_HOOK_PAYLOAD="$(python3 -c '
import json, sys
payload = {
    "requestId": sys.argv[1],
    "prefix": sys.argv[2],
    "promptKey": sys.argv[3],
    "baseBranch": sys.argv[5],
}
repo_url = sys.argv[4]
if repo_url:
    payload["repoUrl"] = repo_url
print(json.dumps(payload))
' "$REQUEST_ID" "$PREFIX" "$PROMPT_KEY" "$REPO_URL" "$BASE_BRANCH")"

# 事前バリデーション: --run-hook-payload に渡す JSON 文字列の UTF-8 バイト長を測り、
# 4096 バイトを超える場合は VM を起動する前に stderr にエラー(実バイト数・超過バイト数・
# 削減ヒント)を出して exit 1 する。これは GitHub issue #3 への保険。
PAYLOAD_BYTES="$(printf '%s' "$RUN_HOOK_PAYLOAD" | wc -c)"
if (( PAYLOAD_BYTES > 4096 )); then
  echo "ERROR: runHookPayload が 4096 バイトを超過しています(実測 ${PAYLOAD_BYTES} バイト, 超過 $(( PAYLOAD_BYTES - 4096 )) バイト)。" >&2
  echo "       ヒント: requestId / baseBranch / repoUrl を短くするか、prompt 本文は promptKey 経由(S3)で渡しているため payload 自体に長い文字列を積まないでください。" >&2
  exit 1
fi

if [[ -n "$REPO_URL" ]]; then
  echo "[run] request=$REQUEST_ID prefix=s3://$RESULT_BUCKET/$PREFIX promptKey=s3://$RESULT_BUCKET/$PROMPT_KEY repo=$REPO_URL base=$BASE_BRANCH"
else
  echo "[run] request=$REQUEST_ID prefix=s3://$RESULT_BUCKET/$PREFIX promptKey=s3://$RESULT_BUCKET/$PROMPT_KEY"
fi

# 非同期モデル: /run は Kiro をバックグラウンド起動して即 200 を返す。Kiro は
# MicroVM が生きている限り裏で走り、完了時に status.json を S3 に put する。
# --maximum-duration-in-seconds が MicroVM の寿命 = Kiro 実行時間を担保する。
# 単調関係: KIRO_TIMEOUT_SECONDS(hook_server 既定 1200)
#          < --maximum-duration(1800) < ホストポーリング上限(1860s)。
MICROVM_ID="$(aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" \
  --execution-role-arn "$EXEC_ROLE_ARN" \
  --egress-network-connectors "[\"$INTERNET_EGRESS_ARN\"]" \
  --run-hook-payload "$RUN_HOOK_PAYLOAD" \
  --maximum-duration-in-seconds 1800 \
  --query microvmId --output text)"
echo "[run] microvm=$MICROVM_ID"

trap 'aws lambda-microvms terminate-microvm --microvm-identifier "$MICROVM_ID" >/dev/null 2>&1 || true' EXIT

# バックグラウンドの Kiro が status.json を put するのを待つ(最大 1860s = 372 x 5s)。
# --maximum-duration(1800s)より少し長くし、VM が寿命で終わるまで待てるようにする。
# status.json は成否に関わらず実行完了後に必ず出るので完了条件にする。
STATUS_KEY="${PREFIX}status.json"
FOUND=""
for _ in $(seq 1 372); do
  if aws s3api head-object --bucket "$RESULT_BUCKET" --key "$STATUS_KEY" >/dev/null 2>&1; then
    FOUND="$STATUS_KEY"
    break
  fi
  sleep 5
done
[[ -n "$FOUND" ]] || { echo "ERROR: status.json なし。CloudWatch Logs(/aws/lambda-microvms/kiro-microvm)を確認。" >&2; exit 1; }

LOCAL_DIR="$LOCAL_DIR_BASE/$REQUEST_ID"
mkdir -p "$LOCAL_DIR"
aws s3 sync "s3://$RESULT_BUCKET/$PREFIX" "$LOCAL_DIR/" >/dev/null

OK="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("ok"))' "$LOCAL_DIR/status.json" 2>/dev/null || echo "?")"
EXIT_CODE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("exitCode"))' "$LOCAL_DIR/status.json" 2>/dev/null || echo "?")"

echo "[run] 完了: ok=$OK exitCode=$EXIT_CODE -> $LOCAL_DIR/"

# GitHub フローの作業報告(report.json / report.md)の有無を表示する。
for R in report.json report.md; do
  if [[ -f "$LOCAL_DIR/$R" ]]; then
    echo "[run] 作業報告: $LOCAL_DIR/$R あり"
  fi
done
