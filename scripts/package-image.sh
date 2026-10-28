#!/usr/bin/env bash
# packages/image を zip 化し、内容ハッシュ付きキーで artifact バケットへアップロードする。
# ハッシュを packages/infra/artifact-hash.txt に書き出し、ImageStack がそれを読んで
# codeArtifact.uri に反映する。zip が変われば キー(=URI)が変わり、CFN が差分を
# 検知して再ビルドする(CLI の update-microvm-image は使わない)。
#
# 使い方:
#   AWS_PROFILE=work ./scripts/package-image.sh
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
PROFILE="${AWS_PROFILE:-work}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_DIR="$ROOT/packages/image"
HASH_FILE="$ROOT/packages/infra/artifact-hash.txt"

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-kiro-microvm-artifact-${ACCOUNT}-${REGION}}"

echo "[package-image] artifact bucket = $ARTIFACT_BUCKET"

TMP_ZIP="$(mktemp -t app-image-XXXXXX).zip"
trap 'rm -f "$TMP_ZIP"' EXIT

echo "[package-image] zip 化: $IMAGE_DIR"
# 決定的な zip にするため、ファイルを名前順で固定し timestamp を揃える。
( cd "$IMAGE_DIR" && find . -type f ! -name '*.DS_Store' | LC_ALL=C sort \
    | zip -q -X "$TMP_ZIP" -@ )

# 内容ハッシュ(入力ファイル群から算出。zip メタデータのブレを避ける)
HASH="$(cd "$IMAGE_DIR" && find . -type f ! -name '*.DS_Store' | LC_ALL=C sort \
    | xargs cat | shasum -a 256 | cut -c1-16)"
KEY="code/app-${HASH}.zip"

echo "[package-image] hash = $HASH"
echo "[package-image] アップロード: s3://$ARTIFACT_BUCKET/$KEY"
aws s3 cp "$TMP_ZIP" "s3://$ARTIFACT_BUCKET/$KEY"

echo "$HASH" > "$HASH_FILE"
echo "[package-image] ハッシュを記録: $HASH_FILE"
echo "[package-image] 完了。次に: cdk deploy KiroMicrovmImage"
