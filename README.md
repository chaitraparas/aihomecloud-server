# AiHomeCloud Backend

The self-hosted server behind AiHomeCloud — a private cloud for your family's photos,
videos, and files, running on hardware you already own instead of a subscription you
rent. FastAPI + SQLite-free JSON storage, built for a Raspberry Pi-class board or any
spare Linux machine.

This backend is AGPL-3.0 licensed — see [LICENSE](LICENSE). You can run it, read it,
modify it, and build your own client against it (see [api-contracts.md](api-contracts.md)
for the API contract). If you run a modified version as a network service for others,
AGPL requires you to make that modified source available to them — running it in your
own home triggers no such obligation.

The officially maintained client apps (Android, and the bundled web UI) are a separate,
proprietary layer built on top of this open server — see the root repository's
[LICENSE](../LICENSE) for the full per-directory breakdown.

## Quick Start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

## Full Setup

See [setup-instructions.md](setup-instructions.md) for a complete install on real
hardware (systemd service, mDNS discovery, TLS, sudoers scoping) rather than the
quick-start above, which is for local development only.

## Tests

```bash
cd backend
python -m pytest tests/ -q
```

## Docs

| Doc | Description |
|-----|-------------|
| [setup-instructions.md](setup-instructions.md) | Full deployment guide (dev + production) |
| [api-contracts.md](api-contracts.md) | API reference — all endpoints, methods, auth |
| [architecture.md](architecture.md) | System architecture — routes, models, providers |
| [changelog.md](changelog.md) | Release history |
