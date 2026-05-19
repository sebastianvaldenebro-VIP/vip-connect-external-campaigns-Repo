#!/usr/bin/env bash
# Deploy api-plans Lambda.
# NOTE: /home/devaju/.local/bin/zip is a broken Python wrapper — always use
# the python3 packaging below, which correctly excludes __pycache__.
set -euo pipefail

FUNCTION_NAME="vip-admin-ui-api-plans"
REGION="us-east-1"
ZIP="/tmp/api-plans.zip"
SRC="$(cd "$(dirname "$0")/src" && pwd)"

echo "Packaging $SRC → $ZIP"
cd "$SRC"
python3 - << 'PYEOF'
import zipfile, pathlib, os

src = pathlib.Path(".")
out = "/tmp/api-plans.zip"

with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for f in sorted(src.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        zf.write(f, f.relative_to(src))
        print(f"  {f.stat().st_size:>7}B  {f.relative_to(src)}")

print(f"\nZip: {os.path.getsize(out):,} bytes  →  {out}")
PYEOF

echo ""
echo "Deploying to $FUNCTION_NAME ..."
AWS_PROFILE=production aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file "fileb://$ZIP" \
  --region "$REGION" \
  --query '{FunctionName:FunctionName,CodeSize:CodeSize,LastModified:LastModified}' \
  --output json

echo ""
AWS_PROFILE=production aws lambda wait function-updated \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION"

echo "Done."
