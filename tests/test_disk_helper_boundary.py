"""
The privileged disk helpers must validate their target at the privilege boundary.

These run as root from polkit-authorized systemd templates whose instance name the unprivileged
service supplies, so the device path is caller-controlled. Before the fix they validated nothing:
`ahc-format-nas.sh` ran `mkfs.ext4 -F ... "$1"` directly, and what looks like validation in
`ahc-partition-format-nas.sh` (`case "$DISK"`) only computes the p1-vs-1 partition suffix.

The API endpoints do refuse OS partitions and mounted devices — but polkit grants the capability to
the service USER, so anything with code execution as that user starts the unit directly and never
touches a line of application code. Application-layer validation is therefore not the boundary.

These are static checks on the shipped shell, because the behavioural tests need real block
devices, lsblk and findmnt — they run against hardware (see `scripts/test-device-guard.sh` and the
results recorded in the audit). What is asserted here is the property those behavioural tests
depend on: **that every destructive command operates on the guard's validated output, never on the
caller's raw argument.** That is what regressed before, and it is checkable without a kernel.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

#: helper -> (guard function it must call, commands that must never see a raw argument)
GUARDED_HELPERS = {
    "ahc-format-nas.sh":            ("ahc_require_destructive_target", ["mkfs.ext4"]),
    "ahc-partition-format-nas.sh":  ("ahc_require_destructive_target", ["sgdisk", "mkfs.ext4"]),
    "ahc-mount-nas.sh":             ("ahc_require_mount_source",       ["mount"]),
}

RAW_ARGS = ('"$1"', "$1", '"$2"')


def _body(name: str) -> str:
    return (SCRIPTS / name).read_text()


@pytest.mark.parametrize("script", sorted(GUARDED_HELPERS))
def test_helper_sources_the_shared_guard(script):
    body = _body(script)
    assert "ahc-device-guard.sh" in body, f"{script} does not load the device guard"


@pytest.mark.parametrize("script", sorted(GUARDED_HELPERS))
def test_missing_guard_is_fatal_rather_than_skipped(script):
    """
    Sourcing must be mandatory.

    A helper that carries on when the guard file is absent is unguarded on exactly the boards where
    deployment went wrong — the worst possible time to fall back to permissive behaviour.
    """
    body = _body(script)
    line = next(l for l in body.splitlines() if "ahc-device-guard.sh" in l and l.strip().startswith("."))
    assert "exit" in line, f"{script} continues when the guard is missing: {line.strip()}"


@pytest.mark.parametrize("script", sorted(GUARDED_HELPERS))
def test_helper_calls_its_guard_function(script):
    fn, _ = GUARDED_HELPERS[script]
    assert fn in _body(script), f"{script} never calls {fn}()"


@pytest.mark.parametrize("script", sorted(GUARDED_HELPERS))
def test_destructive_commands_never_take_the_raw_caller_argument(script):
    """
    THE regression test.

    Against the vulnerable implementation this fails: `mkfs.ext4 -F -L ... "$1"` and
    `mount -o ... "$1" "$2"` passed the caller's string straight through. Every destructive command
    must now operate on the variable the guard returned, which is the canonicalised, validated
    device.
    """
    fn, commands = GUARDED_HELPERS[script]
    offenders = []
    for line in _body(script).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        for cmd in commands:
            if not stripped.startswith(cmd):
                continue
            # `mount ... "$2"` is the MOUNTPOINT, fixed by the unit, not the device — allowed.
            args = stripped[len(cmd):]
            if cmd == "mount":
                args = args.replace('"$2"', "")
            if any(raw in args for raw in RAW_ARGS):
                offenders.append(stripped)
    assert not offenders, (
        f"{script}: destructive command uses the caller's raw argument instead of the "
        f"guard-validated device: {offenders}"
    )


def test_guard_defines_both_entry_points_and_canonicalises():
    guard = _body("ahc-device-guard.sh")
    for fn in ("ahc_canonical_block_device", "ahc_require_destructive_target",
               "ahc_require_mount_source"):
        assert f"{fn}()" in guard, f"guard is missing {fn}()"
    # Canonicalisation is what makes the check ours rather than borrowed from findmnt's internals.
    assert "realpath -e" in guard, "guard does not canonicalise the caller's path"
    # A real block device, not a regular file — `mount` loop-attaches files.
    assert "-b " in guard, "guard does not require a block device"


def test_every_instance_controlled_unit_reaches_a_self_validating_helper():
    """
    Any template whose instance the caller chooses must invoke a helper that validates it ITSELF.

    Written to catch the NEXT instance, not just the known ones. The earlier version of this test
    consulted a hard-coded dict and silently skipped anything not in it — which is precisely how
    `ahc-mount-nas.sh` went unguarded while sitting behind two instance-controlled templates. A
    test that can only confirm what you already listed cannot find the thing you missed.

    "Self-validating" means the helper either loads the shared device guard or refuses bad input on
    its own (a `case` allowlist, a `Usage:` bail-out, an explicit refusal). Application-layer checks
    do not count: polkit grants these units to the service user, so the API is bypassable.
    """
    systemd = SCRIPTS / "systemd"
    offenders = []
    for unit in sorted(systemd.glob("*@.service")):
        exec_line = next((l for l in unit.read_text().splitlines()
                          if l.startswith("ExecStart=")), "")
        if "%I" not in exec_line and "%i" not in exec_line:
            continue                                   # instance not passed to the helper
        target = next((Path(tok).name for tok in exec_line.split() if tok.endswith(".sh")), None)
        if target is None:
            continue                                   # not a shell helper
        script = SCRIPTS / target
        if not script.exists():
            offenders.append(f"{unit.name} -> {target} (helper not found in scripts/)")
            continue
        body = script.read_text()
        validates = (
            "ahc-device-guard.sh" in body
            or "_ahc_refuse" in body
            or "refusing" in body
            or "Usage:" in body
        )
        if not validates:
            offenders.append(f"{unit.name} -> {target} (no self-validation)")
    assert not offenders, (
        "instance-controlled units whose helper does not validate its own input: " + str(offenders)
    )
