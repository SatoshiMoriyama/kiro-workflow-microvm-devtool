#!/usr/bin/env bash
# One-command deploy: Base stack -> zip upload -> Image stack.
# This order resolves the chicken-and-egg problem (CfnMicrovmImage requires the
# zip, and the image build runs during deploy) and makes image content changes
# trigger a rebuild.
#
# Usage:
#   AWS_PROFILE=<your-profile> pnpm deploy
#   AWS_PROFILE=<your-profile> pnpm run deploy   # same
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[deploy] 1/3 Deploying BaseStack"
pnpm --filter infra exec cdk deploy KiroMicrovmBase --require-approval never

echo "[deploy] 2/3 Zipping the image and uploading it to the artifact bucket"
"$ROOT/scripts/package-image.sh"

echo "[deploy] 3/3 Deploying ImageStack (the image build runs here)"
pnpm --filter infra exec cdk deploy KiroMicrovmImage --require-approval never

echo "[deploy] Done. To run a task: kiro-microvm run \"<prompt>\""
