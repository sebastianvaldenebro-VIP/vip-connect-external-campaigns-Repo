#!/usr/bin/env bash
# services/api-progressive-dialer/deploy.sh
set -euo pipefail
PROFILE="${AWS_PROFILE:-production}"
REGION="${AWS_REGION:-us-east-1}"
FUNCTION_CONSUMER="vip-admin-progressive-dialer-consumer"
FUNCTION_CALLER="vip-admin-progressive-dialer-caller"
FUNCTION_SEEDER="vip-admin-progressive-dialer-seeder"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"

echo "Building package..."
pip install -r "$DIR/requirements.txt" -t "$TMP" -q \
  --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
cp -r "$DIR/src/"* "$TMP/"
# vip_shared is provided by the Lambda layer (attached by CDK) — do NOT bundle it here
# to avoid version skew between the layer and the bundled copy.

cd "$TMP"
zip -r /tmp/progressive_dialer.zip . -q

echo "Deploying consumer..."
aws lambda update-function-code \
  --function-name "$FUNCTION_CONSUMER" \
  --zip-file fileb:///tmp/progressive_dialer.zip \
  --region "$REGION" --profile "$PROFILE" --output json | jq '.FunctionName, .CodeSize'

echo "Deploying caller..."
aws lambda update-function-code \
  --function-name "$FUNCTION_CALLER" \
  --zip-file fileb:///tmp/progressive_dialer.zip \
  --region "$REGION" --profile "$PROFILE" --output json | jq '.FunctionName, .CodeSize'

echo "Deploying seeder..."
aws lambda update-function-code \
  --function-name "$FUNCTION_SEEDER" \
  --zip-file fileb:///tmp/progressive_dialer.zip \
  --region "$REGION" --profile "$PROFILE" --output json | jq '.FunctionName, .CodeSize'

rm -rf "$TMP" /tmp/progressive_dialer.zip
echo "Done."
