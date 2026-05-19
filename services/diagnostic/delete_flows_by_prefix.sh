#!/usr/bin/env bash
#
# Delete every Amazon Connect contact flow whose name starts with a given
# prefix. Runs in dry-run mode by default — pass `apply` as the first arg to
# actually delete.
#
# Usage:
#   ./delete_flows_by_prefix.sh                 # dry-run, default prefix "flow-"
#   ./delete_flows_by_prefix.sh apply           # delete after "DELETE" confirm
#   PREFIX=test- ./delete_flows_by_prefix.sh    # override prefix
#
# Env overrides:
#   INSTANCE_ID   Connect instance ID  (default: repo's prod instance)
#   PREFIX        Name prefix to match (default: "flow-")
#   AWS_REGION    (default: us-east-1)
#   AWS_PROFILE   (required for auth — e.g. "production")
#
set -euo pipefail

INSTANCE_ID="${INSTANCE_ID:-6b3f17ba-68a4-472a-9b20-db1991507009}"
PREFIX="${PREFIX:-flow-}"
REGION="${AWS_REGION:-us-east-1}"
MODE="${1:-dry-run}"

if [[ "$MODE" != "dry-run" && "$MODE" != "apply" ]]; then
  echo "Usage: $0 [dry-run|apply]" >&2
  exit 1
fi

echo "Instance:  $INSTANCE_ID"
echo "Prefix:    $PREFIX"
echo "Region:    $REGION"
echo "Profile:   ${AWS_PROFILE:-<default>}"
echo "Mode:      $MODE"
echo ""

# list-contact-flows returns all types when --contact-flow-types is omitted.
# JMESPath filter keeps us under API Gateway response limits even on large instances.
matches=$(aws connect list-contact-flows \
  --instance-id "$INSTANCE_ID" \
  --region "$REGION" \
  --output json \
  --query "ContactFlowSummaryList[?starts_with(Name, \`$PREFIX\`)].[Id,ContactFlowType,Name]" \
  | python3 -c "import json,sys; [print('\t'.join(r)) for r in json.load(sys.stdin)]")

if [ -z "$matches" ]; then
  echo "No flows matched prefix '$PREFIX'."
  exit 0
fi

count=$(echo "$matches" | wc -l | tr -d ' ')
echo "Found $count flows matching '$PREFIX':"
printf '%-36s  %-20s  %s\n' "ID" "TYPE" "NAME"
echo "$matches" | awk -F'\t' '{printf "%-36s  %-20s  %s\n", $1, $2, $3}'
echo ""

if [ "$MODE" = "dry-run" ]; then
  echo "(dry-run — nothing deleted. Re-run with 'apply' to actually delete.)"
  exit 0
fi

read -rp "Type 'DELETE' to confirm deleting all $count flows: " confirm
[ "$confirm" = "DELETE" ] || { echo "Aborted." >&2; exit 1; }

echo ""
ok=0
fail=0
failures=()
while IFS=$'\t' read -r id type name; do
  printf '%-28s %s ... ' "[$type]" "$name"
  if out=$(aws connect delete-contact-flow \
      --instance-id "$INSTANCE_ID" \
      --contact-flow-id "$id" \
      --region "$REGION" 2>&1); then
    echo "OK"
    ok=$((ok+1))
  else
    reason=$(echo "$out" | head -1 | cut -c1-120)
    echo "FAIL — $reason"
    fail=$((fail+1))
    failures+=("$id|$name|$reason")
  fi
  # Basic throttling protection — Connect allows ~2 TPS on write APIs by default.
  sleep 0.25
done <<< "$matches"

echo ""
echo "Result: $ok deleted, $fail failed."
if [ $fail -gt 0 ]; then
  echo ""
  echo "Failures:"
  printf '%s\n' "${failures[@]}"
fi
