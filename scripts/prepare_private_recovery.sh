#!/usr/bin/env bash
# Download, validate and stage the private recovery snapshot.
# GH_TOKEN must be supplied by the caller. This script never writes to the
# remote recovery branch; it only reads GitHub Contents API responses and
# copies the validated AI-memory bytes into the ephemeral bot checkout.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PRIVATE_REPO="$(printenv PRIVATE_REPO 2>/dev/null || printf '%s' 'mishraarvind463-del/xaut-bot')"
PRIVATE_BRANCH="$(printenv PRIVATE_BRANCH 2>/dev/null || printf '%s' 'xaut-recovery-state')"
BOT_WORKSPACE="$(printenv BOT_WORKSPACE 2>/dev/null || printf '%s' 'private-bot')"
GH_TOKEN="$(printenv GH_TOKEN 2>/dev/null || true)"

if [ -z "$GH_TOKEN" ]; then
  echo "GH_TOKEN is required to read private recovery state." >&2
  exit 1
fi

if [ ! -d "$BOT_WORKSPACE" ]; then
  echo "Bot workspace does not exist: $BOT_WORKSPACE" >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Checking read access to the private recovery repository and branch."
if ! api_error="$(gh api "repos/$PRIVATE_REPO/branches/$PRIVATE_BRANCH" 2>&1 >/dev/null)"; then
  echo "GitHub API access check failed for $PRIVATE_REPO branch $PRIVATE_BRANCH." >&2
  # Print the CLI's HTTP/authentication diagnostic, but never expose a token.
  printf '%s\n' "$api_error" | sed -E \
    -e 's/github_pat_[A-Za-z0-9_]+/[REDACTED]/g' \
    -e 's/gh[pousr]_[A-Za-z0-9_]+/[REDACTED]/g' >&2
  exit 1
fi

fetch_private_file() {
  local remote_path="$1"
  local local_path="$2"
  gh api "repos/$PRIVATE_REPO/contents/$remote_path?ref=$PRIVATE_BRANCH" \
    --jq '.content' | tr -d '\n' | base64 --decode > "$local_path"
  test -s "$local_path"
}

fetch_private_file "runtime-state/PRIVATE_STORAGE_MIGRATION.json" "$TMP_DIR/manifest.json"
fetch_private_file "runtime-state/bot_recovery_state.json" "$TMP_DIR/bot_recovery_state.json"
fetch_private_file "runtime-state/trade_history.csv" "$TMP_DIR/trade_history.csv"
fetch_private_file "runtime-state/ai_memory.json" "$TMP_DIR/ai_memory.json"
fetch_private_file "runtime-state/shadow_trade_journal.jsonl.gz" "$TMP_DIR/shadow_trade_journal.jsonl.gz"

# Schema-v4 verified-only migrations carry the exact original v3 bytes as a
# separate immutable archive. Fetch it only for that migration type; legacy
# schema-v1/v2/v3 and native-v4 snapshots do not require this extra object.
migration_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("migration", {}).get("id", ""))' "$TMP_DIR/ai_memory.json")"
if [ "$migration_id" = "verified-only-v1" ]; then
  fetch_private_file "runtime-state/ai_memory_v3_archive.json" "$TMP_DIR/ai_memory_v3_archive.json"
fi

python "$SCRIPT_DIR/validate_recovery_state.py" "$TMP_DIR"

# Only stage AI memory after every downloaded recovery file passes validation.
cp "$TMP_DIR/ai_memory.json" "$BOT_WORKSPACE/ai_memory.json"
echo "Verified private AI memory staged in runner workspace."
