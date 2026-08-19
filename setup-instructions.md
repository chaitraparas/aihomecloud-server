# AiHomeCloud — Setup & Deployment Instructions

> Last verified: 2026-03-23 on Radxa ROCK Pi 4A (Armbian Ubuntu 24.04)

---

## Recommended: Interactive Setup Wizard

For non-technical users setting up a device for the first time:

```bash
ssh paras@<device-ip>
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/nerdyparas/AiHomeCloud.git ~/AiHomeCloud
cd ~/AiHomeCloud
sudo bash scripts/setup-wizard.sh
```

The wizard walks through device naming, admin PIN creation, optional storage setup, and starts all services — in about 3 minutes.

---

## Part 1: Quick Start — New Hardware (Clone + One Command)

For developers or users who prefer command-line setup, use `dev-setup.sh`:

This is the standard path for any ARM64 SBC running Ubuntu/Debian.
Tested on: **Radxa ROCK Pi 4A** (RK3399, Armbian 26.2 Noble, kernel 6.18-rockchip64).

### Prerequisites

- SBC running Ubuntu 24.04 (Noble) or Debian Bookworm, 64-bit ARM
- SSH access (or direct terminal)
- User with sudo rights (e.g., `paras`)
- Network connection (Ethernet recommended)

### Step 1 — SSH into the board

```bash
# Find the IP from your router, or use the hostname if mDNS is already working
ssh paras@192.168.x.x
```

### Step 2 — Install git and clone the repo

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/<your-org>/AiHomeCloud.git ~/AiHomeCloud
cd ~/AiHomeCloud
```

### Step 3 — Run dev-setup.sh

```bash
sudo bash scripts/dev-setup.sh
```

This single command handles everything (idempotent — safe to re-run):

| Step | What it does |
|------|-------------|
| 1 | Installs required packages: `avahi-daemon`, `avahi-utils`, `openssl`, `python3`, `python3-venv`, `lsof`, `curl` |
| 2 | Deploys Avahi mDNS service → app discovers device in ~1 s instead of 30 s |
| 3 | Creates Python venv at `backend/.venv/` and installs all Python dependencies |
| 4 | Creates `/var/lib/aihomecloud/` (data dir) and `/srv/nas/` (NAS folders), owned by your user |
| 5 | Generates device serial (`AHC-<hostname>-<MAC-last4>`) and a random pairing key |
| 6 | Installs `/etc/systemd/system/aihomecloud.service` (system-level, survives reboot) |
| 7 | Disables any conflicting user-level service (`~/.config/systemd/user/`) |
| 8 | Adds `/etc/sudoers.d/aihomecloud` — passwordless sudo for mount/umount/mkfs/systemctl |
| 9 | Removes `evil_link` path-traversal artifact if left by pytest in `/srv/nas/` |
| 10 | Starts the service and runs a health check |

**Expected output at the end:**
```
[AiHomeCloud] Health check PASSED ✓
Backend URL  : https://192.168.1.100:8443
Device serial: AHC-EXAMPLE-1234
Health check : curl -sk https://localhost:8443/api/health
mDNS type    : _aihomecloud-nas._tcp (Flutter fast discovery)
```

### Step 4 — Verify

```bash
# Health check
curl -sk https://localhost:8443/api/health
# → {"status": "ok"}

# Identity check (what the app probes)
curl -sk https://localhost:8443/
# → {"service":"AiHomeCloud","version":"0.1.0","deviceName":"...","serial":"AHC-..."}

# mDNS advertisement
avahi-browse -t _aihomecloud-nas._tcp
# → + end0 IPv4 AiHomeCloud on rockpi-4a _aihomecloud-nas._tcp local

# Service status
sudo systemctl status aihomecloud
```

---

## System Packages — Why Each is Needed

| Package | Required? | Purpose |
|---------|-----------|---------|
| `python3`, `python3-venv`, `python3-pip` | ✅ Required | Backend runtime |
| `openssl` | ✅ Required | Auto-generates self-signed TLS cert on first boot |
| `avahi-daemon` | ✅ Required | mDNS — `_aihomecloud-nas._tcp` for instant app discovery |
| `avahi-utils` | ✅ Required | `avahi-browse` for verifying mDNS broadcasts |
| `lsof` | ✅ Required | Open file-handle check before unmount |
| `curl` | ✅ Required | Health-check script and backend HTTP probes |
| `samba` | Optional | SMB file sharing (Windows/Mac) |
| `nfs-kernel-server` | Optional | NFS file sharing (Linux/Mac) |
| `minidlna` | Optional | DLNA media streaming for smart TVs |
| `tesseract-ocr` | Optional | OCR for document indexing |
| `poppler-utils` | Optional | PDF text extraction (`pdftotext`) for document indexing |

> **Do NOT skip avahi-daemon.** Without it, the Flutter app falls back to a full /24 TCP subnet
> scan which takes 30+ seconds. With avahi + mDNS, discovery takes ~1 second.

---

## File Locations After Setup

```
/home/<user>/AiHomeCloud/          # Git repo — backend source + Flutter source
  backend/
    .venv/                         # Python venv (created by dev-setup.sh)
    app/                           # FastAPI backend source
    requirements.txt               # Python dependencies

/etc/systemd/system/
  aihomecloud.service              # System-level service (written by dev-setup.sh)

/etc/sudoers.d/
  aihomecloud                      # Passwordless sudo rules (written by dev-setup.sh)

/etc/avahi/services/
  aihomecloud.service              # mDNS advertisement (deployed by dev-setup.sh)

/var/lib/aihomecloud/              # Persistent data (owned by login user, chmod 750)
  jwt_secret                       # Auto-generated JWT signing key (first boot)
  pairing_key                      # Generated by dev-setup.sh, persisted across reinstalls
  users.json                       # User accounts
  storage.json                     # Mount state
  services.json                    # NAS service toggles
  tokens.json                      # Refresh token records
  device.json                      # Device display name
  tls/
    cert.pem                       # Self-signed TLS certificate (auto-generated)
    key.pem                        # TLS private key

/srv/nas/                          # NAS folders (owned by login user)
  personal/                        # Per-user private folders
  family/                          # Family shared folder
  entertainment/
    Movies/  Music/  ...           # Entertainment subfolders
```

---

## Why sudo -n is Critical (backend invariant)

All `run_command(["sudo", ...])` calls in the backend use the `-n` (non-interactive) flag.

**Without `-n`**: If sudo requires a password, the call blocks for 30 seconds waiting on a TTY
that doesn't exist (backend runs as a daemon), then fails. This causes tests to hang and storage
operations to silently time out in production.

**With `-n`**: If no NOPASSWD rule exists, sudo exits immediately with rc=1. The error is logged
and returned to the caller — no blocking.

**Lesson from 2026-03-15**: All 23 sudo calls in `storage_helpers.py`, `storage_routes.py`,
`system_routes.py`, and `service_routes.py` were missing `-n`, causing test hangs. The fix:
```python
# Always use -n in run_command() calls:
await run_command(["sudo", "-n", "mount", device, mount_point])
#                        ^^^^
```
The `/etc/sudoers.d/aihomecloud` file installed by `dev-setup.sh` grants NOPASSWD for all
storage-related commands so they succeed in production.

---

## Part 2: Managing the Service

```bash
# Check status
sudo systemctl status aihomecloud

# View live logs
sudo journalctl -u aihomecloud -f

# Restart after code changes
sudo systemctl restart aihomecloud

# Stop
sudo systemctl stop aihomecloud

# Re-run setup after a git pull that adds new Python dependencies
sudo bash scripts/dev-setup.sh    # (idempotent — only runs pip install if needed)
```

---

## Part 3: Production Deploy (first-boot-setup.sh)

For a clean multi-user production deployment (creates a dedicated `aihomecloud` system user,
installs to `/opt/aihomecloud/`, sets up polkit for NetworkManager):

```bash
sudo bash scripts/first-boot-setup.sh
```

> **Note:** This is designed for the Radxa Cubie A7Z production target. For dev on any SBC,
> use `scripts/dev-setup.sh` instead.

---

## Part 4: QR Code Pairing Flow

### How pairing works

The Flutter app finds the backend via:
1. mDNS (`_aihomecloud-nas._tcp`) — resolves in ~1 second if avahi is running
2. Subnet scan (TCP port 8443) — fallback, takes ~30 seconds on a /24

Once found, the app probes `GET /` and checks `json['service'] == 'AiHomeCloud'`.

The pairing flow:
```
App GET /api/v1/pair/qr  →  { qrValue, serial, ip, host }
App POST /api/v1/pair    →  { token }  (using serial + pairing_key)
App stores: JWT token, device serial, host, TLS cert fingerprint
```

### Getting the pairing credentials

```bash
# Get the QR payload (and decode it with jq or Python)
curl -sk https://localhost:8443/api/v1/pair/qr | python3 -m json.tool

# Fast pair via curl (no QR needed for dev)
curl -sk -X POST https://localhost:8443/api/v1/pair \
  -H "Content-Type: application/json" \
  -d '{"serial":"AHC-EXAMPLE-1234","key":"<pairing_key>"}'
# pairing_key is in /var/lib/aihomecloud/pairing_key
```

### QR payload format
```
aihomecloud://pair?serial=AHC-EXAMPLE-1234&key=<key>&host=rockpi-4a.local&expiresAt=<unix>
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `curl: Failed to connect` | Service not running | `sudo systemctl status aihomecloud` → check logs |
| App can't find device (30s scan) | avahi-daemon not installed or service file missing | `sudo bash scripts/dev-setup.sh` |
| App can't find device (even after scan) | Backend not running on port 8443 | `curl -sk https://localhost:8443/`; check service |
| `403 Unknown serial` | Serial mismatch | Check `AHC_DEVICE_SERIAL` in service file vs what app has |
| `403 Invalid pairing key` | Key mismatch | Check `cat /var/lib/aihomecloud/pairing_key` |
| Storage ops return error immediately | `sudo -n` failing — NOPASSWD rule missing | `sudo bash scripts/dev-setup.sh` to install sudoers rule |
| Tests hang for minutes | sudo calls blocking on TTY (missing `-n` flag) | All `run_command(["sudo", ...])` must use `"-n"` |
| `board_unknown` in logs | Board not in `board.py` known boards | Add entry to `KNOWN_BOARDS` and `_BOARD_SUBSTRINGS` in `board.py` |
| `/srv/nas/evil_link` exists | pytest `test_path_safety.py` bug (pre-2026-03-15) | `rm /srv/nas/evil_link` or run `dev-setup.sh` |
| Backend won't start after git pull | New Python deps added | `cd backend && .venv/bin/pip install -r requirements.txt` |
| User-level and system-level service conflict | Both `~/.config/systemd/user/` and `/etc/systemd/system/` services exist | `dev-setup.sh` disables the user-level one; or run it manually |
| `RuntimeWarning: coroutine was never awaited` | asyncio loop lifecycle issue in tests | Normal in test teardown for subprocess transport; not a test failure |

---

## Running Tests

```bash
cd backend
AHC_DATA_DIR=/tmp/ahc_test AHC_SKIP_MOUNT_CHECK=true \
    .venv/bin/python -m pytest tests/ -q \
    --ignore=tests/test_hardware_integration.py

# Expected: all pass (256+ tests)
# test_hardware_integration.py requires real hardware — skip in CI
```

**Why `AHC_SKIP_MOUNT_CHECK=true`?** On dev hardware, `/srv/nas` is a plain directory (not a
USB mount point). This env var tells the backend not to fail startup because no storage device
is mounted.
