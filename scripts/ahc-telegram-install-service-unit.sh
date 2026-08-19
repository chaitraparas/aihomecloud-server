#!/bin/sh
#
# ahc-root-input: generate
# GENERATES the unit from validated parameters. It used to copy a unit the service user had
# authored straight into /etc/systemd/system, which was a full RCE-to-root. (C-1)
# Runs OUTSIDE the aihomecloud sandbox (as root, via a systemd oneshot unit).
#
# GENERATES the telegram-bot-api unit. It must never COPY one.
#
# It used to copy /var/lib/aihomecloud/telegram_setup_staging/telegram-bot-api.service into
# /etc/systemd/system/ verbatim. That directory is owned by the unprivileged `aihomecloud` user, and
# polkit lets that same user start the resulting unit — so anything able to write as the service
# user (an RCE in the backend, a path-traversal write, a bug in an upload handler) could author a
# unit with any ExecStart it liked and have root run it. A complete privilege escalation with no
# preconditions, found in the 2026-08-08 audit.
#
# The staged content was never actually variable: the backend built it entirely from its own
# constants, and the API credentials travel separately in an EnvironmentFile precisely so they never
# appear in a world-readable unit. So generating it here costs nothing and removes the class of bug.
# Nothing under /var/lib/aihomecloud is read by this script.
set -e

APP_USER=aihomecloud
# The service user's own directory, not /usr/local/bin. This unit already runs the binary AS
# that user, so root installing it bought no isolation — it only meant root wrote an executable
# whose bytes an unprivileged process had chosen. Keeping it where the owner already has write
# access removes the boundary instead of guarding it. Kept in step with _BINARY_PATH in
# app/routes/telegram_routes.py.
BINARY=/var/lib/aihomecloud/bin/telegram-bot-api
PORT=8081
DATA_DIR=/var/lib/aihomecloud/telegram-bot-api
ENV_FILE=/var/lib/aihomecloud/telegram-bot-api.env
DEST=/etc/systemd/system/telegram-bot-api.service

cat > "$DEST" <<UNIT
[Unit]
Description=Telegram Local Bot API Server
After=network.target

[Service]
User=${APP_USER}
Restart=always
RestartSec=5
EnvironmentFile=${ENV_FILE}
# telegram-bot-api refuses to start if --dir does not already exist; recreate on every start so the
# service is self-healing if the directory is removed independently (seen live 2026-07-15,
# crash-looping silently for 34+ hours).
ExecStartPre=/bin/mkdir -p ${DATA_DIR}
# Wrapped in sh -c because systemd does not substitute EnvironmentFile variables embedded
# mid-argument when ExecStart invokes the binary directly.
ExecStart=/bin/sh -c "exec ${BINARY} --api-id=\$AHC_TG_API_ID --api-hash=\$AHC_TG_API_HASH --http-port=${PORT} --dir=${DATA_DIR} --local"
Environment=HOME=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNIT

chmod 0644 "$DEST"
# Leave no attacker-authored file lying around for a future version to trust.
rm -f /var/lib/aihomecloud/telegram_setup_staging/telegram-bot-api.service
systemctl daemon-reload
