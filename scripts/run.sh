#!/usr/bin/env bash
# MicroVM を起動し、/run フックが S3 に吐いた結果を回収して終了する。
#
# 前提: cdk deploy 済み + scripts/package-image.sh でイメージビルド済み。
# 使い方:
#   AWS_PROFILE=work ./scripts/run.sh
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
PROFILE="${AWS_PROFILE:-work}"
STACK="${STACK_NAME:-KiroMicrovmPhase1Base}"
IMAGE_NAME="${MICROVM_IMAGE_NAME:-kiro-phase1}"

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

echo "[run] スタック出力を取得..."
get_output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
RESULT_BUCKET="$(get_output ResultBucketName)"
EXEC_ROLE_ARN="$(get_output ExecutionRoleArn)"

if [[ -z "$RESULT_BUCKET" || "$RESULT_BUCKET" == "None" ]]; then
  echo "[run] ERROR: ResultBucketName を解決できない。cdk deploy 済みか確認する。" >&2
  exit 1
fi
echo "[run] result bucket   = $RESULT_BUCKET"
echo "[run] execution role  = $EXEC_ROLE_ARN"

# イメージ ARN を名前から解決(SUCCESSFUL かつ ACTIVE な最新版が前提)
IMAGE_ARN="$(aws lambda-microvms list-microvm-images \
  --query "items[?name=='$IMAGE_NAME'].imageArn | [0]" --output text)"
if [[ -z "$IMAGE_ARN" || "$IMAGE_ARN" == "None" ]]; then
  echo "[run] ERROR: イメージ '$IMAGE_NAME' が見つからない。ビルド完了を確認する。" >&2
  exit 1
fi
echo "[run] image arn       = $IMAGE_ARN"

INTERNET_EGRESS_ARN="arn:aws:lambda:${REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
echo "[run] run-microvm..."
RUN_JSON="$(aws lambda-microvms run-microvm \
  --image-identifier "$IMAGE_ARN" \
  --execution-role-arn "$EXEC_ROLE_ARN" \
  --egress-network-connectors "[\"$INTERNET_EGRESS_ARN\"]" \
  --maximum-duration-in-seconds 300)"
MICROVM_ID="$(echo "$RUN_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("microvmId") or json.load(sys.stdin).get("microvmIdentifier",""))' 2>/dev/null || true)"
if [[ -z "$MICROVM_ID" ]]; then
  # フィールド名が不明な場合に備えて生 JSON を表示
  echo "[run] run-microvm response:"; echo "$RUN_JSON"
  MICROVM_ID="$(echo "$RUN_JSON" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(next((v for k,v in d.items() if "icrovm" in k and isinstance(v,str)), ""))')"
fi
echo "[run] microvm id      = $MICROVM_ID"

cleanup() {
  if [[ -n "${MICROVM_ID:-}" ]]; then
    echo "[run] terminate-microvm $MICROVM_ID"
    aws lambda-microvms terminate-microvm --microvm-identifier "$MICROVM_ID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# /run フックが S3 に result-*.txt を吐くのを待つ
echo "[run] result を S3 でポーリング(最大 90s)..."
FOUND=""
for i in $(seq 1 45); do
  KEY="$(aws s3api list-objects-v2 --bucket "$RESULT_BUCKET" \
    --prefix "result-" --query "sort_by(Contents,&LastModified)[-1].Key" \
    --output text 2>/dev/null || true)"
  if [[ -n "$KEY" && "$KEY" != "None" ]]; then
    FOUND="$KEY"; break
  fi
  sleep 2
done

if [[ -z "$FOUND" ]]; then
  echo "[run] ERROR: 結果が現れなかった。CloudWatch Logs(/aws/lambda-microvms/kiro-phase1)を確認する。" >&2
  exit 1
fi

echo "[run] 結果ファイル: s3://$RESULT_BUCKET/$FOUND"
echo "----- 内容 -----"
aws s3 cp "s3://$RESULT_BUCKET/$FOUND" -
echo "----------------"
echo "[run] 成功: MicroVM が起動し S3 に結果を書いた。"
