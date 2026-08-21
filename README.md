<p align="center">
  <h1 align="center">AiHomeCloud Backend</h1>
  <p align="center">A private cloud for your family's photos, videos, and files —<br>running on hardware you already own, not a subscription you rent.</p>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_v3-blue.svg?style=for-the-badge&label=License" alt="License: AGPLv3"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <a href="https://github.com/chaitraparas/aihomecloud-server/stargazers"><img src="https://img.shields.io/github/stars/chaitraparas/aihomecloud-server?style=for-the-badge&color=yellow" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="https://aihomecloud.com">Website</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="api-contracts.md">API Reference</a> ·
  <a href="setup-instructions.md">Setup Guide</a> ·
  <a href="#license--open-source">License</a>
</p>

---

FastAPI + SQLite-free JSON storage, built to run on a Raspberry Pi-class board (or any spare
Linux/Windows machine) as the always-on server for the AiHomeCloud family NAS. No monthly bill,
no vendor storage cap, no third party ever touching the files — the board sitting on your shelf
is the whole infrastructure.

## Why AiHomeCloud

- **Own the hardware, own the data.** Runs on a Radxa/Rock Pi-class SBC, an old laptop, or a
  Windows PC — whatever you already have. No cloud storage bill, ever.
- **Real remote access, no relay.** Embedded [Tailscale](https://tailscale.com) gives every
  paired device a direct, end-to-end encrypted path home — AiHomeCloud never sits in the middle
  of your traffic, and never could even if it wanted to.
- **Multi-profile family accounts.** Personal, family-shared, and entertainment libraries with
  per-user PINs — not one shared login for the whole household.
- **Finds what you're looking for.** Local semantic search (on-device embedding model, nothing
  sent anywhere) alongside classic filename/text search — free for everyone, not a paid tier.
- **Talks to you on Telegram.** Upload, search, and browse straight from a self-hosted Telegram
  bot using your own bot token — zero AiHomeCloud-operated infrastructure in that path either.
- **Cleans up after itself.** Nightly exact + perceptual-hash duplicate scanning finds wasted
  space across every profile, with a one-tap review flow instead of a script you have to trust.
- **TLS and identity done properly.** Per-board key pinning with scheduled rotation (not a
  static self-signed cert you click through once and forget), a low-privilege service account,
  and a real installer on both Linux and Windows — not a `curl | sudo bash` and a prayer.

AiHomeCloud isn't trying to be everything — no plugin marketplace, no app store, no general
compute platform. It's built for one job: your family's photos, videos, and files, staying
yours, forever. If you want a do-everything self-hosted suite, Nextcloud already does that well;
this is deliberately smaller and more opinionated.

## Quick Start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

This starts the API locally for development. For a real install on actual hardware —
systemd service, mDNS discovery, TLS, a scoped low-privilege service account — see
**[Full Setup](#full-setup)** below.

## Full Setup

See [setup-instructions.md](setup-instructions.md) for a complete production install
(systemd service, mDNS discovery, TLS, sudoers scoping) rather than the quick-start
above, which is for local development only.

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

## License & open source

This backend is **AGPL-3.0** licensed — see [LICENSE](LICENSE). You can run it, read it,
modify it, and build your own client against it (the [API reference](api-contracts.md) above
documents every endpoint). If you run a modified version as a network service for others, AGPL
requires you to make that modified source available to them — running it in your own home
triggers no such obligation.

The officially maintained client apps (Android, and the bundled web UI) are a separate,
proprietary layer built on top of this open server — see the root repository's `LICENSE` for
the full per-directory breakdown.

---

<p align="center">
  If this saves you a subscription, <a href="https://github.com/chaitraparas/aihomecloud-server/stargazers">a star</a> helps more people find it.
</p>
