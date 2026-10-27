#!/usr/bin/env bash
# MicroVM を起動し、/run フックが S3 に吐いた結果を回収して終了する。
#
# 前提: cdk deploy 済み + scripts/package-image.sh でイメージビルド済み。
# 使い方:
#   AWS_PROFILE=work ./scripts/run.sh
#   AWS_PROFILE=work PROMPT="東京の天気を調べて weather.html を作って" ./scripts/run.sh
#   AWS_PROFILE=work ./scripts/run.sh "東京の天気を調べて weather.html を作って"
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
PROFILE="${AWS_PROFILE:-work}"
STACK="${STACK_NAME:-KiroMicrovmPhase1Base}"
IMAGE_NAME="${MICROVM_IMAGE_NAME:-kiro-phase1}"

# prompt は PROMPT 環境変数 or 第1引数で受け、両方無ければデフォルト(hook_server と同一)。
DEFAULT_PROMPT="札幌の天気を調べて weather.html を作って"
PROMPT="${PROMPT:-${1:-$DEFAULT_PROMPT}}"

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
PREFIX="runs/${REQUEST_ID}/"
INTERNET_EGRESS_ARN="arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
RUN_HOOK_PAYLOAD="$(python3 -c 'import json,sys;print(json.dumps({"requestId":sys.argv[1],"prefix":sys.argv[2],"prompt":sys.argv[3]}))' "$REQUEST_ID" "$PREFIX" "$PROMPT")"

echo "[run] request=$REQUEST_ID prefix=s3://$RESULT_BUCKET/$PREFIX"

MICROVM_ID="$(aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" \
  --execution-role-arn "$EXEC_ROLE_ARN" \
  --egress-network-connectors "[\"$INTERNET_EGRESS_ARN\"]" \
  --run-hook-payload "$RUN_HOOK_PAYLOAD" \
  --maximum-duration-in-seconds 1200 \
  --query microvmId --output text)"
echo "[run] microvm=$MICROVM_ID"

trap 'aws lambda-microvms terminate-microvm --microvm-identifier "$MICROVM_ID" >/dev/null 2>&1 || true' EXIT

# /run フックが status.json を put するのを待つ(最大 1260s = 252 x 5s)。
# status.json は成否に関わらず実行完了後に必ず出るので完了条件にする。
STATUS_KEY="${PREFIX}status.json"
FOUND=""
for _ in $(seq 1 252); do
  if aws s3api head-object --bucket "$RESULT_BUCKET" --key "$STATUS_KEY" >/dev/null 2>&1; then
    FOUND="$STATUS_KEY"
    break
  fi
  sleep 5
done
[[ -n "$FOUND" ]] || { echo "ERROR: status.json なし。CloudWatch Logs(/aws/lambda-microvms/kiro-phase1)を確認。" >&2; exit 1; }

LOCAL_DIR="$LOCAL_DIR_BASE/$REQUEST_ID"
mkdir -p "$LOCAL_DIR"
aws s3 sync "s3://$RESULT_BUCKET/$PREFIX" "$LOCAL_DIR/" >/dev/null

OK="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("ok"))' "$LOCAL_DIR/status.json" 2>/dev/null || echo "?")"
EXIT_CODE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("exitCode"))' "$LOCAL_DIR/status.json" 2>/dev/null || echo "?")"

echo "[run] 完了: ok=$OK exitCode=$EXIT_CODE -> $LOCAL_DIR/"
