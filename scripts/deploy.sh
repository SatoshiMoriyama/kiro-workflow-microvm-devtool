#!/usr/bin/env bash
# Base -> zip 投入 -> Image の順で 1 コマンドデプロイする。
# 鶏卵問題(CfnMicrovmImage は zip を要求)と、イメージ中身変更時の再ビルドを
# この順序で吸収する。
#
# 使い方:
#   AWS_PROFILE=work pnpm deploy
#   AWS_PROFILE=work pnpm run deploy        # 同じ
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[deploy] 1/3 BaseStack をデプロイ"
pnpm --filter infra exec cdk deploy KiroMicrovmPhase1Base --require-approval never

echo "[deploy] 2/3 image を zip 化して artifact バケットへ投入"
"$ROOT/scripts/package-image.sh"

echo "[deploy] 3/3 ImageStack をデプロイ(ここでイメージビルドが走る)"
pnpm --filter infra exec cdk deploy KiroMicrovmPhase1Image --require-approval never

echo "[deploy] 完了。実行は: AWS_PROFILE=work pnpm run-microvm"
