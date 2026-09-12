# AiHomeCloud — Setup & Deployment Instructions

Covers this repo's actual installers: `install.sh` (Linux) and `install_windows.ps1`
(Windows). Both are the single, real entry point — there is no separate
wizard/dev-setup/production-deploy split.

---

## Linux (Ubuntu/Debian/Armbian — aarch64, armv7l, or x86_64)

`install.sh` is a universal, idempotent installer (safe to re-run; it never overwrites
existing config, secrets, or user data):

```bash
git clone https://github.com/chaitraparas/aihomecloud-server.git
cd aihomecloud-server
sudo bash install.sh
```

Or without cloning first:

```bash
curl -sSL https://install.aihomecloud.app | sudo bash
```

Optional flags:

| Flag | Effect |
|------|--------|
| `--keep-desktop` | Skip removing Chromium/X.Org/SDDM (default: removed on headless boards; auto-skipped anyway if a real graphical session is detected) |
| `--enable-firewall` | Actually enable the LAN-scoped ufw firewall the installer prepares. Rules are written and validated either way; only the `ufw enable` call itself defaults off — `ufw` has had real, board-specific kernel incompatibilities (see `configure_firewall()`'s own comments) |

The installer creates a dedicated, unprivileged `aihomecloud` system user; installs the
backend to `/opt/aihomecloud/`; and sets up the systemd service, mDNS advertisement,
sudoers/polkit scoping, TLS certificate issuance, storage auto-mount, and (optionally)
the firewall. See `install.sh`'s `main()` for the complete phase list.

### Verify

```bash
curl -k https://localhost:8443/api/health
sudo systemctl status aihomecloud
sudo journalctl -u aihomecloud -f
```

### Uninstall

```bash
sudo bash /usr/local/bin/ahc-factory-reset.sh [--purge] [--wipe-media]
```

## Windows

```powershell
.\install_windows.ps1
```

See that script's own header comments for supported Windows versions and options.

---

## File Locations (Linux, after `install.sh`)

```
/opt/aihomecloud/
  backend -> backend_releases/<version>/  # symlink to the current release
  backend_releases/<version>/
    .venv -> ../../shared-venv/           # shared across releases
    app/                                  # FastAPI backend source
    requirements.txt / requirements-arm64.txt
  shared-venv/                            # one Python venv, reused by every release

/etc/systemd/system/
  aihomecloud.service                     # system-level service

/etc/sudoers.d/
  aihomecloud                             # passwordless sudo, scoped to storage/service commands

/var/lib/aihomecloud/                     # persistent data (owned by the aihomecloud user)
  jwt_secret
  pairing_key
  users.json
  storage.json
  tls/
    cert.pem
    key.pem

/srv/nas/                                 # NAS folders
  personal/
  family/
  entertainment/
```

---

## Why `sudo -n` is Critical (backend invariant)

Every `run_command(["sudo", ...])` call in the backend uses the `-n` (non-interactive) flag.

**Without `-n`**: if sudo requires a password, the call blocks for 30 seconds waiting on a
TTY that doesn't exist (the backend runs as a daemon), then fails — this hangs tests and
silently times out storage operations in production.

**With `-n`**: if no NOPASSWD rule exists, sudo exits immediately with `rc=1`; the error is
logged and returned to the caller — no blocking.

The sudoers rule `install.sh` writes to `/etc/sudoers.d/aihomecloud` grants NOPASSWD for
exactly the storage/service commands the backend needs, nothing broader.

---

## QR Code Pairing Flow

The client app finds the backend via mDNS (`_aihomecloud._tcp`) or a LAN scan fallback,
then probes `GET /` and checks the returned service identity.

```
App GET /api/v1/pair/qr  →  { qrValue, serial, ip, host }
App POST /api/v1/pair    →  { token }   (using serial + pairing_key)
```

```bash
# Get the QR payload
curl -sk https://localhost:8443/api/v1/pair/qr | python3 -m json.tool

# Pair via curl directly (no QR needed for dev)
curl -sk -X POST https://localhost:8443/api/v1/pair \
  -H "Content-Type: application/json" \
  -d '{"serial":"AHC-EXAMPLE-1234","key":"<pairing_key>"}'
# pairing_key is in /var/lib/aihomecloud/pairing_key
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `curl: Failed to connect` | Service not running | `sudo systemctl status aihomecloud` → check logs |
| App can't find the device | avahi-daemon not installed/running | `sudo systemctl status avahi-daemon`; re-run `sudo bash install.sh` |
| `403 Unknown serial` | Serial mismatch | Compare the board's device serial against what the app has |
| `403 Invalid pairing key` | Key mismatch | `cat /var/lib/aihomecloud/pairing_key` |
| Storage ops return an error immediately | `sudo -n` failing — NOPASSWD rule missing | Re-run `sudo bash install.sh` (idempotent) to reinstall the sudoers rule |
| Tests hang for minutes | A `run_command(["sudo", ...])` call is missing `-n` | Every sudo call in the backend must pass `-n` |
| `RuntimeWarning: coroutine was never awaited` | asyncio loop lifecycle in test teardown | Normal for subprocess-transport teardown; not a test failure |

---

## Running Tests

```bash
python -m pytest -q
```

Requires the dependencies from `requirements.txt` (or `requirements-arm64.txt` on ARM)
installed in the active environment. `tests/test_hardware_integration.py` self-skips
(`pytest.mark.skipif`) unless it's actually running against a live, reachable backend —
no separate flag needed to exclude it in a normal dev environment.
