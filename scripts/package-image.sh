#!/usr/bin/env bash
# packages/image を zip 化して artifact バケットの固定キーへアップロードする。
# BaseStack デプロイ後、ImageStack デプロイ前に実行する。
#
# 使い方:
#   AWS_PROFILE=work ./scripts/package-image.sh
#
# artifact バケットは固定名 kiro-phase1-artifact-<account>-<region>。
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
PROFILE="${AWS_PROFILE:-work}"
ARTIFACT_KEY="code/app.zip"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_DIR="$ROOT/packages/image"

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-kiro-phase1-artifact-${ACCOUNT}-${REGION}}"

echo "[package-image] artifact bucket = $ARTIFACT_BUCKET"

TMP_ZIP="$(mktemp -t app-image-XXXXXX).zip"
trap 'rm -f "$TMP_ZIP"' EXIT

echo "[package-image] zip 化: $IMAGE_DIR -> $TMP_ZIP"
( cd "$IMAGE_DIR" && zip -r -q "$TMP_ZIP" . -x '*.DS_Store' )

echo "[package-image] アップロード: s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"
aws s3 cp "$TMP_ZIP" "s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"

echo "[package-image] 完了: s3://$ARTIFACT_BUCKET/$ARTIFACT_KEY"
