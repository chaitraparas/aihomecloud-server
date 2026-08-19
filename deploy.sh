#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# AiHomeCloud deploy script
#
# Pushes backend code to the AiHomeCloud device, installs ARM64-pinned dependencies,
# restarts the systemd service, and verifies via health check.
#
# Required env vars:
#   TARGET_HOST   - AiHomeCloud device host or IP (e.g. 192.168.1.100)
#
# Optional env vars:
#   TARGET_USER   - SSH user on the device (default: aihomecloud)
#   TARGET_PORT   - API port (default: 8443)
#   AHC_CERT    - Path to server certificate PEM for TLS verification
#   DEPLOY_DIR    - Remote install dir (default: /opt/aihomecloud)
# -----------------------------------------------------------------------------

TARGET_HOST="${TARGET_HOST:-}"
TARGET_USER="${TARGET_USER:-aihomecloud}"
TARGET_PORT="${TARGET_PORT:-8443}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/aihomecloud}"

if [[ -z "$TARGET_HOST" ]]; then
  echo "ERROR: TARGET_HOST is required"
  echo "Usage: TARGET_HOST=192.168.1.100 ./deploy.sh"
  exit 1
fi

REMOTE="${TARGET_USER}@${TARGET_HOST}"
HEALTH_URL="https://${TARGET_HOST}:${TARGET_PORT}/api/health"

# --- 1. Sync backend code ---------------------------------------------------
echo "==> Syncing backend to ${REMOTE}:${DEPLOY_DIR}/backend ..."
rsync -az --delete \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude 'tests/' \
  --exclude 'build/' \
  backend/ "${REMOTE}:${DEPLOY_DIR}/backend/"

# --- 2. Install ARM64-pinned dependencies -----------------------------------
echo "==> Installing ARM64 dependencies ..."
ssh "${REMOTE}" "cd ${DEPLOY_DIR} && \
  python3 -m pip install --quiet --requirement backend/requirements-arm64.txt"

# --- 3. Restart service ------------------------------------------------------
echo "==> Restarting aihomecloud service ..."
ssh "${REMOTE}" "sudo systemctl restart aihomecloud"
sleep 3

# --- 4. Health check ---------------------------------------------------------
echo "==> Checking backend health at: ${HEALTH_URL}"

if [[ -n "${AHC_CERT:-}" ]]; then
  echo "    Using CA cert: ${AHC_CERT}"
  curl --fail --silent --show-error --max-time 10 \
    --cacert "${AHC_CERT}" \
    "${HEALTH_URL}" >/dev/null
else
  echo "    WARNING: AHC_CERT is not set â€” using insecure TLS bypass (-k)."
  echo "    WARNING: This should only be used for local/dev workflows."
  curl --fail --silent --show-error --max-time 10 \
    -k \
    "${HEALTH_URL}" >/dev/null
fi

echo "==> Deploy complete. Health check passed."
