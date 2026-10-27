#!/usr/bin/env bash
# packages/image を zip 化して artifact バケットの固定キーへアップロードする。
# CfnMicrovmImage の codeArtifact.uri はこの zip を指すので、cdk deploy の前に実行する。
#
# 使い方:
#   AWS_PROFILE=work ARTIFACT_BUCKET=<bucket> ./scripts/package-image.sh
#
# ARTIFACT_BUCKET を省略した場合はデプロイ済みスタックの出力から取得する。
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
PROFILE="${AWS_PROFILE:-work}"
STACK="${STACK_NAME:-KiroMicrovmPhase1}"
ARTIFACT_KEY="code/app.zip"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_DIR="$ROOT/packages/image"

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

if [[ -z "${ARTIFACT_BUCKET:-}" ]]; then
  echo "[package-image] ARTIFACT_BUCKET 未指定。スタック出力から取得する..."
  ARTIFACT_BUCKET="$(aws cloudformation describe-stacks \
    --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='ArtifactBucketName'].OutputValue" \
    --output text)"
fi

if [[ -z "$ARTIFACT_BUCKET" || "$ARTIFACT_BUCKET" == "None" ]]; then
  echo "[package-image] ERROR: ARTIFACT_BUCKET を解決できない。先に cdk deploy するか ARTIFACT_BUCKET を指定する。" >&2
  exit 1
fi

TMP_ZIP="$(mktemp -t app-image-XXXXXX.zip)"
trap 'rm -f "$TMP_ZIP"' EXIT

echo "[package-image] zip 化: $IMAGE_DIR -> $TMP_ZIP"
( cd "$IMAGE_DIR" && zip -r -q "$TMP_ZIP" . -x '*.DS_Store' )

echo "[package-image] アップロード: s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"
aws s3 cp "$TMP_ZIP" "s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"

echo "[package-image] 完了: s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"
