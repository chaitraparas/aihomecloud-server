#!/usr/bin/env bash
# Builds a clean, allowlisted local staging copy of backend/ as it would ship in the
# public `aihomecloud-server` repo. Local-only: never pushes, never touches a remote.
#
# Deliberately NOT a `git subtree split`. A subtree export carries full history for every
# commit that touched backend/ -- backend/ used to hold internal audit/critique/blueprint
# notes (moved out 2026-08-19, see docs/backend-internal/) whose *past* versions would
# still be in that history even after today's HEAD is clean. This script instead produces
# a fresh, history-free tree snapshot; the caller is expected to `git init` a single
# "Initial public release" commit from it. Once the first export is out and backend/ has
# no more historical contamination risk, subtree (or a rerun of this same script into an
# existing clone, committed normally) is fine for ongoing sync.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST="${1:?Usage: export_public_server.sh <destination-dir>}"

rm -rf "$DEST"
mkdir -p "$DEST"

# Allowlist: only these paths ship. Anything not listed here stays private by default,
# same rule as the root LICENSE file's own "everything not covered is proprietary".
ALLOW_DIRS=(app tests scripts systemd)
ALLOW_FILES=(
    LICENSE README.md .gitignore
    requirements.txt requirements-arm64.txt requirements.in
    pytest.ini run_tests.sh
    install.sh install_windows.ps1 deploy.sh aihomecloud.service
    api-contracts.md architecture.md changelog.md setup-instructions.md
)
# Excluded even though they sit under an allowlisted dir: scripts/stage_webapp.sh
# references clients/web/ (the proprietary web client, outside backend/ entirely) --
# meaningless and revealing of private repo structure in a server-only public checkout.
EXCLUDE_PATHS=(scripts/stage_webapp.sh)

for d in "${ALLOW_DIRS[@]}"; do
    [[ -d "$BACKEND_DIR/$d" ]] || continue
    mkdir -p "$DEST/$d"
    rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
        "$BACKEND_DIR/$d/" "$DEST/$d/"
done

for f in "${ALLOW_FILES[@]}"; do
    [[ -f "$BACKEND_DIR/$f" ]] && cp "$BACKEND_DIR/$f" "$DEST/$f"
done

for x in "${EXCLUDE_PATHS[@]}"; do
    rm -f "$DEST/$x"
done

echo "Exported allowlisted backend/ tree -> $DEST"
echo "Files: $(find "$DEST" -type f | wc -l | tr -d ' ')"
