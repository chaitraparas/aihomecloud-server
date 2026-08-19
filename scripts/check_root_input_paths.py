#!/usr/bin/env python3
"""
Fail CI when a root helper reads a path the unprivileged service user can write.

Six CRITICALs in the August 2026 audit were the same bug: a script running as root consuming
content authored by the `aihomecloud` service user. The systemd unit installer, the log mirror, the
binary installer, the hotspot config, the WiFi keyfile — each found separately, each fixed
separately, and two of them only turned up because someone swept the whole class by hand.

This makes the next one fail a build instead. It is deliberately dumb: any root script mentioning a
service-writable root must carry an explicit marker saying which of the three sanctioned patterns it
uses. The marker is not a suppression — writing one forces the author to name the mechanism, and a
reviewer to check the claim.

    # ahc-root-input: generate   — root builds the artifact from validated parameters; the caller
    #                              supplies values, never structure.
    # ahc-root-input: validate   — root re-checks caller-supplied values itself and REFUSES bad
    #                              input rather than coercing it. Upstream validation does not
    #                              count: the boundary has to check itself.
    # ahc-root-input: none       — the path is only removed/created, never read as input.

There is no "trusted" marker on purpose. If root genuinely does not need to cross the boundary, the
answer is to delete the crossing, as the telegram binary installer did — not to annotate it.

Usage:  python3 backend/scripts/check_root_input_paths.py [--root <repo root>]
Exit:   0 clean, 1 findings.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The service's own ReadWritePaths= entries — everything it can write without privilege.
#: Keep in step with aihomecloud.service.
SERVICE_WRITABLE = (
    "/var/lib/aihomecloud",
    "/srv/nas",
    "/mnt/ahc_backup",
    "/opt/aihomecloud/update_staging",
)

VALID_MARKERS = ("generate", "validate", "none")
_MARKER_RE = re.compile(r"#\s*ahc-root-input:\s*(\w+)")


def scan(scripts_dir: Path) -> list[str]:
    problems: list[str] = []
    for script in sorted(scripts_dir.glob("*.sh")):
        text = script.read_text(encoding="utf-8", errors="replace")

        hits = sorted({p for p in SERVICE_WRITABLE if p in text})
        if not hits:
            continue

        markers = _MARKER_RE.findall(text)
        if not markers:
            problems.append(
                f"{script.relative_to(scripts_dir.parents[1])}: references service-writable "
                f"{', '.join(hits)} but declares no `# ahc-root-input:` marker.\n"
                f"    Root must never consume content this service user authored. Either delete "
                f"the crossing, or declare which pattern applies: "
                f"{', '.join(VALID_MARKERS)}."
            )
            continue

        bad = [m for m in markers if m not in VALID_MARKERS]
        if bad:
            problems.append(
                f"{script.relative_to(scripts_dir.parents[1])}: unknown marker "
                f"{', '.join(repr(b) for b in bad)} (expected one of {', '.join(VALID_MARKERS)})."
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()

    scripts_dir = args.root / "backend" / "scripts"
    if not scripts_dir.is_dir():
        print(f"error: no scripts directory at {scripts_dir}", file=sys.stderr)
        return 1

    problems = scan(scripts_dir)
    if problems:
        print("Root helpers consuming service-writable paths without a declared pattern:\n")
        for p in problems:
            print(f"  - {p}\n")
        print(f"{len(problems)} script(s) need attention. See this file's docstring for the "
              f"three sanctioned patterns.")
        return 1

    print("OK — every root helper touching a service-writable path declares its pattern.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
