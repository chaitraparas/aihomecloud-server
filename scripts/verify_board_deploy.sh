#!/usr/bin/env bash
# Detects drift between this local checkout's backend/app/ and what's actually deployed on a
# board. Doesn't change the deploy mechanism itself (rsync/scp file-by-file, per this repo's
# CLAUDE.md -- the board is deliberately not kept as a clean git checkout, see that file's
# "Deploy" section for why) -- this is a read-only, zero-risk companion: run it after any deploy
# to confirm exactly what you intended to ship is what's actually running, rather than assuming.
#
# Found live 2026-07-16 (full-repo audit): a large fraction of this project's real bugs traced
# back to "the board had stale code" discovered only by accident. This makes that discoverable
# on purpose, on demand, instead of by accident.
#
# Usage: scripts/verify_board_deploy.sh <user@host>
#   e.g. scripts/verify_board_deploy.sh user@192.168.1.100

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <user@host>" >&2
  exit 1
fi

REMOTE="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$BACKEND_DIR/app"

# The board's checkout root differs by board (see backend/CLAUDE.md) -- ~/AiHomeCloud/backend is
# the convention on both boards this project currently targets (symlinked from
# /opt/aihomecloud/backend), so this is not hardcoded to one specific board's path beyond that.
REMOTE_APP_DIR="AiHomeCloud/backend/app"

echo "Comparing local $APP_DIR against $REMOTE:~/$REMOTE_APP_DIR ..."
echo

# Local manifest: relative-path + sha256, one per line, sorted -- .bak-* files and __pycache__
# are deploy/runtime artifacts, never part of what should match the source tree.
local_manifest=$(cd "$APP_DIR" && find . -name '*.py' -not -path '*/__pycache__/*' -print0 \
  | xargs -0 shasum -a 256 \
  | sed 's#\./##' \
  | sort)

# Remote manifest: same shape, computed on the board over SSH. sha256sum (GNU coreutils, present
# on both Debian and Ubuntu) rather than shasum -a 256 (macOS/BSD) -- the two produce identical
# output format, so the diff below compares apples to apples.
remote_manifest=$(ssh "$REMOTE" "cd $REMOTE_APP_DIR && find . -name '*.py' -not -path '*/__pycache__/*' -not -name '*.bak-*' -print0 \
  | xargs -0 sha256sum \
  | sed 's#\./##' \
  | sort")

local_paths=$(echo "$local_manifest" | awk '{print $2}' | sort)
remote_paths=$(echo "$remote_manifest" | awk '{print $2}' | sort)
missing_on_board=$(comm -23 <(echo "$local_paths") <(echo "$remote_paths"))
extra_on_board=$(comm -13 <(echo "$local_paths") <(echo "$remote_paths"))
# Re-sort both manifests by path (not by hash, which is what `sort` on the raw manifest lines
# above actually does) before diffing -- otherwise two trees with the exact same file set in a
# different hash-sort order produce a wall of spurious line-position mismatches.
local_by_path=$(echo "$local_manifest" | sort -k2)
remote_by_path=$(echo "$remote_manifest" | sort -k2)
hash_mismatches=$(diff <(echo "$local_by_path") <(echo "$remote_by_path") | grep '^[<>]' || true)

drift_found=0

if [ -n "$missing_on_board" ]; then
  drift_found=1
  echo "FILES IN REPO BUT NOT ON BOARD:"
  echo "$missing_on_board" | sed 's/^/  /'
  echo
fi

if [ -n "$extra_on_board" ]; then
  drift_found=1
  echo "FILES ON BOARD BUT NOT IN REPO (leftover from a removed feature, or hand-edited):"
  echo "$extra_on_board" | sed 's/^/  /'
  echo
fi

if [ -n "$hash_mismatches" ]; then
  drift_found=1
  echo "CONTENT MISMATCHES (same filename, different bytes -- the board is running an older or"
  echo "hand-edited version of these files):"
  echo "$hash_mismatches" | sed 's/^/  /'
  echo
fi

if [ "$drift_found" -eq 0 ]; then
  echo "No drift found -- $REMOTE is running exactly what's in this local checkout's backend/app/."
  exit 0
else
  echo "Drift found. Deploy the files listed above via the scp/stage/py_compile/restart pattern"
  echo "in backend/CLAUDE.md, then re-run this script to confirm."
  exit 1
fi
