"""
WiFi management — keep the WiFi radio live independently of Ethernet.

WiFi and Ethernet are both left connected whenever available (no radio-toggling based
on which interface happens to be up) — this board should just always be reachable,
and a single dropped cable or flaky AP shouldn't take the whole thing offline. The
persisted "desired state" flag only reflects the user's own explicit on/off choice via
the Settings toggle; it defaults to on for a never-configured board.

Uses nmcli (NetworkManager) for WiFi radio control.
Reads /sys/class/net/<iface>/operstate for Ethernet link detection (status-reporting only).
"""

import asyncio
import logging
import uuid
from pathlib import Path

from .subprocess_runner import run_command
from . import store

logger = logging.getLogger("aihomecloud.wifi")

# Persisted desired radio state, set by the user's explicit Settings toggle
# (set_user_wifi_override). Defaults to True: a board that's never been touched should
# just have WiFi on, same as Ethernet.
_user_wifi_override = True
_monitor_task: asyncio.Task | None = None
_MONITOR_INTERVAL = 60  # seconds between self-heal checks

# Where connect_to_network() stages a .nmconnection profile before handing it to the
# ahc-wifi-install@ unit (host-namespace root, see that unit's own comment) to install into
# /etc/NetworkManager/system-connections/ with the right ownership/permissions.
#
# NOT /tmp: aihomecloud.service runs with PrivateTmp=yes, so its own /tmp is a private,
# per-service tmpfs invisible outside that one process's mount namespace -- a file staged there
# literally cannot be seen by the host-namespace ahc-wifi-install@ unit (confirmed live
# 2026-07-14: "install: cannot stat '/tmp/ahc-wifi-<ssid>.nmconnection': No such file or
# directory" even though the Python code had just written it). /var/lib/aihomecloud is one of
# the service's ReadWritePaths= entries -- a real bind-mount from the host filesystem, not a
# private namespace -- so anything staged there is genuinely visible to the escape-hatch unit.
# Matches the exact pattern ahc-telegram-install-binary.sh already uses for the same reason
# (STAGED=/var/lib/aihomecloud/telegram_setup_staging/...).
_STAGING_DIR = Path("/var/lib/aihomecloud/wifi_setup_staging")


def _ethernet_is_up() -> bool:
    """Check if any wired Ethernet interface has carrier (operstate = 'up')."""
    net_dir = Path("/sys/class/net")
    if not net_dir.exists():
        return False
    for iface in net_dir.iterdir():
        name = iface.name
        # Skip loopback, wireless, virtual interfaces
        if name == "lo" or name.startswith("wl") or name.startswith("docker") or name.startswith("veth"):
            continue
        operstate = iface / "operstate"
        if operstate.exists():
            try:
                state = operstate.read_text().strip()
                if state == "up":
                    logger.debug("Ethernet interface %s is up", name)
                    return True
            except OSError:
                continue
    return False


async def disable_wifi() -> bool:
    """Disable WiFi radio via nmcli. Returns True on success."""
    rc, out, err = await run_command(["nmcli", "radio", "wifi", "off"], timeout=10)
    if rc == 0:
        logger.info("WiFi radio disabled")
        return True
    logger.warning("Failed to disable WiFi: %s", err)
    return False


async def enable_wifi() -> bool:
    """Enable WiFi radio via nmcli. Returns True on success."""
    rc, out, err = await run_command(["nmcli", "radio", "wifi", "on"], timeout=10)
    if rc == 0:
        logger.info("WiFi radio enabled")
        return True
    logger.warning("Failed to enable WiFi: %s", err)
    return False


async def get_wifi_status() -> dict:
    """Return current WiFi radio state and Ethernet status."""
    rc, out, err = await run_command(["nmcli", "radio", "wifi"], timeout=10)
    wifi_enabled = out.strip().lower() == "enabled" if rc == 0 else None
    return {
        "wifiEnabled": wifi_enabled,
        "ethernetUp": _ethernet_is_up(),
        "userOverride": _user_wifi_override,
    }


async def ensure_wifi_on_startup() -> None:
    """On startup: bring the WiFi radio to the last explicitly-desired state (on, by
    default). No Ethernet check — both interfaces simply stay live whenever available."""
    global _user_wifi_override
    _user_wifi_override = await store.get_value("wifi_user_override", True)
    if _user_wifi_override:
        logger.info("Bringing WiFi radio on at startup")
        await enable_wifi()
    else:
        logger.info("WiFi left off at startup — user previously turned it off")
        await disable_wifi()


async def _wifi_monitor_loop() -> None:
    """Periodically re-assert the desired WiFi radio state, in case something outside
    this app (a driver hiccup, a manual rfkill) knocked it out of sync. Does not react
    to Ethernet state at all — WiFi and Ethernet are independent."""
    while True:
        await asyncio.sleep(_MONITOR_INTERVAL)
        try:
            status = await get_wifi_status()
            wifi_enabled = status.get("wifiEnabled")
            if wifi_enabled is None:
                continue  # couldn't determine radio state this cycle — try again next tick
            if _user_wifi_override and not wifi_enabled:
                logger.info("WiFi radio unexpectedly off — re-enabling")
                await enable_wifi()
            elif not _user_wifi_override and wifi_enabled:
                logger.info("WiFi radio unexpectedly on — disabling per saved preference")
                await disable_wifi()
        except (OSError, RuntimeError) as exc:
            logger.warning("WiFi monitor check failed: %s", exc)


def start_wifi_monitor() -> None:
    """Start the background WiFi / Ethernet monitor task."""
    global _monitor_task
    _monitor_task = asyncio.create_task(
        _wifi_monitor_loop(), name="wifi_monitor"
    )


async def stop_wifi_monitor() -> None:
    """Cancel the background monitor task on shutdown."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None


async def set_user_wifi_override(enabled: bool) -> None:
    """User explicitly toggles WiFi — set override flag and persist."""
    global _user_wifi_override
    _user_wifi_override = enabled
    await store.set_value("wifi_user_override", enabled)
    if enabled:
        await enable_wifi()
    else:
        await disable_wifi()


def _split_terse_line(line: str) -> list[str]:
    """Split one nmcli `-t` line on unescaped ':', un-escaping \\: and \\\\.

    nmcli's terse mode escapes literal ':' and '\\' within a field with a
    leading backslash -- a naive line.split(":") would silently misparse any
    SSID/connection-name containing a colon.
    """
    fields: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            current.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    fields.append("".join(current))
    return fields


async def scan_networks() -> list[dict]:
    """Scan for available Wi-Fi networks, deduped by SSID (strongest AP wins).

    Returns dicts matching the WifiNetwork model's field names (camelCase
    aliases applied by the route/response_model, not here).
    """
    rc, out, err = await run_command(
        ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"],
        timeout=20,
    )
    if rc != 0:
        logger.warning("Wi-Fi scan failed: %s", err)
        return []

    saved_rc, saved_out, _ = await run_command(["nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=10)
    saved_names = (
        {_split_terse_line(line)[0] for line in saved_out.splitlines() if line.strip()}
        if saved_rc == 0 else set()
    )

    seen: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = _split_terse_line(line)
        if len(fields) < 4:
            continue
        in_use, ssid, signal, security = fields[0], fields[1], fields[2], fields[3]
        if not ssid:
            continue  # hidden network — nothing to show or connect to by name
        try:
            signal_pct = int(signal)
        except ValueError:
            signal_pct = 0
        existing = seen.get(ssid)
        # Two APs can broadcast the same SSID (mesh/dual-band) with NetworkManager connected to
        # whichever one it roamed to, not necessarily the strongest-signal one -- found live
        # 2026-07-14: `nmcli device wifi list` showed Neo6G at both 87% (not in-use) and 70%
        # (in-use, the real active AP). Picking the strongest entry unconditionally silently
        # discarded the in_use flag whenever the connected AP wasn't the strongest, making an
        # actually-connected network show as not-in-use in the scan list (the top status card
        # stayed correct since it queries the active connection directly, but the list entry's
        # checkmark was wrong). Fix: keep the strongest-signal entry as the representative row,
        # but OR in_use across every entry seen for that SSID rather than only the winning one.
        is_in_use = (in_use == "*") or (existing["in_use"] if existing else False)
        if existing is not None and existing["signal"] >= signal_pct:
            existing["in_use"] = is_in_use
            continue
        seen[ssid] = {
            "ssid": ssid,
            "signal": signal_pct,
            "security": security or "Open",
            "in_use": is_in_use,
            "saved": ssid in saved_names,
        }
    return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)


async def get_wifi_connection_info() -> dict:
    """Return the currently-active Wi-Fi connection's SSID + IP, if any."""
    rc, out, _ = await run_command(
        ["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION,DEVICE", "device", "status"], timeout=10,
    )
    if rc == 0:
        for line in out.splitlines():
            fields = _split_terse_line(line)
            if len(fields) < 4:
                continue
            dev_type, state, connection, device = fields[0], fields[1], fields[2], fields[3]
            if dev_type == "wifi" and state == "connected":
                ip_addr = None
                ip_rc, ip_out, _ = await run_command(
                    ["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", device], timeout=10,
                )
                if ip_rc == 0:
                    for ip_line in ip_out.splitlines():
                        ip_fields = _split_terse_line(ip_line)
                        if ip_fields and ip_fields[0].startswith("IP4.ADDRESS") and len(ip_fields) > 1:
                            ip_addr = ip_fields[1].split("/")[0]
                            break
                return {"connected": True, "ssid": connection, "ip": ip_addr}
    return {"connected": False, "ssid": None, "ip": None}


class UnsafeWifiValue(ValueError):
    """An SSID or password that must not be written to a file root will read."""


#: 802.11 caps an SSID at 32 bytes; a WPA2 passphrase at 63. Values beyond these cannot describe
#: a real network, so refusing them costs nothing and keeps the staged files small and bounded.
_MAX_SSID_BYTES = 32
_MAX_PSK_LEN = 63


def _reject_unsafe_value(value: str, *, field: str, max_bytes: int) -> None:
    """
    Refuse a value that would change the STRUCTURE of a file a root helper parses.

    Both staged formats interpolate these values verbatim, and root reads the result:

      * the hotspot config is line-oriented -- iface, ssid, password, in that order -- and the
        helper pulls each with `sed -n '<n>p'`. A newline inside the SSID shifts every field after
        it, so an SSID of "Home\n" makes line 3 (the password) empty and the hotspot comes up
        OPEN while the app reports the password was set. That is a silent downgrade to an
        unprotected network, not a crash.
      * the WiFi profile is a NetworkManager keyfile that root installs into
        /etc/NetworkManager/system-connections/ as 0600 root:root. A newline in the SSID or
        passphrase injects arbitrary keys and sections into a root-owned system config file.

    Both disappear the moment control characters cannot get in, which is what this enforces.
    Refused, never stripped: silently rewriting an SSID produces a profile that cannot match the
    real access point, and the family gets an unexplained "cannot connect" instead of an error
    naming the character. (2026-08-08 audit -- the newline case was missed by the council, found
    while sweeping the root-consumes-service-writable-path class.)
    """
    # `isprintable()` is wrong here: it rejects the plain space that most real SSIDs contain.
    # The structural characters are the C0/C1 control ranges plus DEL.
    bad = {c for c in value if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F}
    if bad:
        shown = ", ".join(repr(c) for c in sorted(bad))
        raise UnsafeWifiValue(f"{field} cannot contain control characters ({shown})")
    if len(value.encode("utf-8")) > max_bytes:
        raise UnsafeWifiValue(f"{field} is too long (max {max_bytes} bytes)")


def _safe_profile_name(ssid: str) -> str:
    """Sanitize an SSID into a name safe to use as both a filename and an `nmcli connection`
    identifier. Deliberately NOT the raw SSID stored in the profile's own [wifi] ssid= field —
    that must stay byte-for-byte exact for NetworkManager to match the real access point;
    this sanitized form is only ever used as our own internal handle (filename stem, `id=`,
    and the argument to `connection up`/`connection delete`)."""
    name = "".join(c for c in ssid if c.isalnum() or c in (" ", "-", "_", ".")).strip()
    return name or "ahc-wifi-network"


async def _systemd_escape(value: str) -> str:
    """Escape a raw string for safe use as a systemd unit *instance* name (the part
    between @ and .service). A bare unit name only permits a narrow charset
    (letters/digits/:-_.\\) — anything else, spaces in particular (routine in real
    SSIDs, and _safe_profile_name deliberately allows them), needs systemd-escape
    first or `systemctl start` fails outright. Found live 2026-07-15: this path was
    passing the raw sanitized name directly, which additionally meant any literal
    hyphen in the SSID got misread as an *encoded slash* by the unit's own %I
    (unescaped instance) — e.g. "My-Home-WiFi" would arrive at the script as
    "My/Home/WiFi". The fix is symmetric: escape here with `systemd-escape`, and
    the .service file's ExecStart keeps %I (not %i) to correctly reverse this exact
    encoding back to the original string — %i would hand the script the still-escaped
    form, which is a different, equally wrong bug.
    """
    rc, out, err = await run_command(["systemd-escape", value], timeout=5)
    if rc != 0:
        raise RuntimeError(f"systemd-escape failed for {value!r}: {err}")
    return out.strip()


async def connect_to_network(ssid: str, password: str) -> dict:
    """Write a NetworkManager connection profile for [ssid]/[password] and activate it.

    Writes the profile directly as a keyfile rather than `nmcli ... password <pwd>` —
    a password passed as a subprocess argument is briefly visible via /proc/<pid>/cmdline
    to any local account on the board; a keyfile written with 0600 permissions never puts
    the password on a command line at all.
    """
    if not ssid.strip():
        return {"success": False, "message": "SSID cannot be empty", "ip": None}
    try:
        _reject_unsafe_value(ssid, field="SSID", max_bytes=_MAX_SSID_BYTES)
        _reject_unsafe_value(password, field="Password", max_bytes=_MAX_PSK_LEN)
    except UnsafeWifiValue as exc:
        return {"success": False, "message": str(exc), "ip": None}

    safe = _safe_profile_name(ssid)
    profile_uuid = str(uuid.uuid4())

    lines = [
        "[connection]",
        f"id={safe}",
        f"uuid={profile_uuid}",
        "type=wifi",
        "autoconnect=true",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={ssid}",
        "",
    ]
    if password:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={password}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", "addr-gen-mode=default", ""]
    content = "\n".join(lines)

    tmp_path = _STAGING_DIR / f"ahc-wifi-{safe}.nmconnection"
    try:
        _STAGING_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content)
        tmp_path.chmod(0o600)
    except OSError as exc:
        return {"success": False, "message": f"Failed to stage connection profile: {exc}", "ip": None}

    # NoNewPrivileges=yes on aihomecloud.service blocks sudo/setuid entirely, regardless of
    # sudoers grants (confirmed live: "sudo: effective uid is not 0") -- installing the staged
    # profile as root has to go through a systemd oneshot unit in the host namespace instead,
    # the same escape hatch ahc-mount@/ahc-umount already use for storage. The helper script
    # (ahc-wifi-install.sh) knows the source/dest paths itself from the %I instance name; it
    # also removes the staged /tmp file, so no unlink needed here on the success path — only
    # clean up ourselves if the unit never got a chance to run.
    #
    # The helper script also runs `nmcli connection reload` itself (as root) after installing
    # the profile -- Settings.ReloadConnections is hardcoded root-only at the D-Bus policy
    # level (upstream NetworkManager bug 1921082), not a polkit-gated action at all, so no
    # .pkla/.rules grant could ever let the unprivileged aihomecloud user call it directly
    # (confirmed live 2026-07-14: "access denied" persisted even with both
    # settings.modify.system and reload actions granted and pkcheck confirming both YES).
    #
    # `safe` must be systemd-escaped before it becomes a unit *instance* name (found
    # live 2026-07-15 — see _systemd_escape's own docstring): a raw instance can't
    # contain a space at all, and a raw hyphen is misread as an encoded slash by the
    # unit's %I. The filename above is unaffected (plain filenames allow both).
    try:
        escaped_safe = await _systemd_escape(safe)
    except RuntimeError as exc:
        logger.warning("Failed to escape Wi-Fi profile name for systemd: %s", exc)
        tmp_path.unlink(missing_ok=True)
        return {"success": False, "message": "Failed to install connection profile", "ip": None}

    rc, _, err = await run_command(
        ["systemctl", "start", f"ahc-wifi-install@{escaped_safe}.service"], timeout=15
    )
    if rc != 0:
        logger.warning("Failed to install Wi-Fi connection profile: %s", err)
        tmp_path.unlink(missing_ok=True)
        return {"success": False, "message": "Failed to install connection profile", "ip": None}

    # Keeps the auto-disable-on-Ethernet monitor from reaping this connection 60s later if the
    # board is still on Ethernet while the user is setting up a WiFi fallback (see wifi_manager
    # module docstring / _wifi_monitor_loop).
    await set_user_wifi_override(True)

    rc, out, err = await run_command(["nmcli", "connection", "up", safe], timeout=30)
    if rc != 0:
        logger.warning("Failed to activate Wi-Fi connection %s: %s", safe, err)
        return {"success": False, "message": err or out or "Failed to connect", "ip": None}

    info = await get_wifi_connection_info()
    return {"success": True, "message": "Connected", "ip": info.get("ip")}


async def forget_network(ssid: str) -> bool:
    """Remove a saved Wi-Fi connection profile."""
    safe = _safe_profile_name(ssid)
    rc, _, err = await run_command(["nmcli", "connection", "delete", safe], timeout=10)
    if rc != 0:
        logger.warning("Failed to forget network %s: %s", ssid, err)
        return False
    return True


# ── Hotspot (item 7) ─────────────────────────────────────────────────────────
#
# `nmcli device wifi hotspot` (present since nmcli ~1.10, confirmed on every board's installed
# version — 1.42.4 on the Cubie A5E, 1.46.0 on the Rock Pi 4A / x86 thin client) starts AP mode
# on a WiFi device directly; no separate `nmcli connection add type wifi mode ap` dance needed.
#
# Live-toggle caveat, confirmed live during this session: neither Rock Pi 4A nor the x86 thin
# client has WiFi hardware at all (`nmcli device status` lists no wifi-type device on either --
# they're Ethernet-only boards). The Cubie A5E is the only board in this fleet with a WiFi
# radio. So `_find_wifi_interface()` returning None is the NORMAL, expected result on the two
# boards this was allowed to be tested against — not a bug.
#
# Root-only D-Bus restriction, found live 2026-07-19 (same class of bug as
# Settings.ReloadConnections, see the wifi-install section's comment above): calling `nmcli
# device wifi hotspot` as the unprivileged aihomecloud user fails with "Not authorized to share
# connections via wifi" -- confirmed NOT a polkit-rule gap (added and tested both
# org.freedesktop.NetworkManager.wifi.share.protected and .open, restarted polkitd, still
# denied), confirmed NOT a radio-state issue (same failure with the WiFi radio administratively
# on), confirmed it succeeds INSTANTLY as root. Fixed the same way as the WiFi-install case:
# stage the parameters and run the actual nmcli call from the ahc-hotspot-enable@ host-namespace
# oneshot unit instead (see ahc-hotspot.sh).

_HOTSPOT_CONNECTION_NAME = "ahc-hotspot"
_HOTSPOT_STAGING_DIR = Path("/var/lib/aihomecloud/hotspot_staging")


async def _find_wifi_interface() -> str | None:
    """First device nmcli reports as TYPE=wifi, or None if this board has no WiFi radio at all
    (Rock Pi 4A / the x86 thin client in this fleet)."""
    rc, out, _ = await run_command(["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"], timeout=10)
    if rc != 0:
        return None
    for line in out.splitlines():
        fields = _split_terse_line(line)
        if len(fields) >= 2 and fields[1] == "wifi":
            return fields[0]
    return None


async def _wait_for_wifi_device_ready(iface: str, attempts: int = 10, delay_seconds: float = 0.5) -> bool:
    """Poll until `iface` leaves the "unavailable" state after `enable_wifi()`.

    Found live 2026-07-19: `nmcli radio wifi on` returns as soon as the command exits, but the
    device itself takes ~2-3s to actually cycle unavailable -> disconnected -> connected
    (measured live on the Cubie A5E). Calling `nmcli device wifi hotspot` immediately after
    `enable_wifi()` with no wait raced ahead of that and failed with "device is not available"
    every time -- a blind `sleep` would work most of the time but either wastes time on a board
    that comes up fast or isn't long enough on a slow one; polling the real device state is the
    only way to know it's actually ready.
    """
    for _ in range(attempts):
        rc, out, _ = await run_command(["nmcli", "-t", "-f", "DEVICE,STATE", "device", "status"], timeout=10)
        if rc == 0:
            for line in out.splitlines():
                fields = _split_terse_line(line)
                if len(fields) >= 2 and fields[0] == iface and fields[1] != "unavailable":
                    return True
        await asyncio.sleep(delay_seconds)
    return False


async def get_hotspot_status() -> dict:
    """Whether this board's WiFi device is currently running the ahc-hotspot connection
    profile (vs. its normal client-mode connection, or no WiFi radio present at all)."""
    iface = await _find_wifi_interface()
    if iface is None:
        return {"adapter_present": False, "enabled": False, "ssid": None}

    rc, out, _ = await run_command(
        ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", iface], timeout=10,
    )
    active_name = None
    if rc == 0:
        for line in out.splitlines():
            fields = _split_terse_line(line)
            if fields and fields[0] == "GENERAL.CONNECTION" and len(fields) > 1:
                active_name = fields[1]

    enabled = active_name == _HOTSPOT_CONNECTION_NAME
    ssid = None
    if enabled:
        rc2, out2, _ = await run_command(
            ["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", _HOTSPOT_CONNECTION_NAME],
            timeout=10,
        )
        if rc2 == 0:
            for line in out2.splitlines():
                fields = _split_terse_line(line)
                if len(fields) > 1:
                    ssid = fields[1]
    return {"adapter_present": True, "enabled": enabled, "ssid": ssid}


async def enable_hotspot(ssid: str, password: str) -> dict:
    """Start AP mode on this board's WiFi device, via the ahc-hotspot-enable@ host-namespace
    root helper (see this section's module comment for why -- `nmcli device wifi hotspot`
    itself is root-only at the D-Bus policy level, not authorizable via polkit for the
    unprivileged aihomecloud user this service runs as).

    The helper script deletes any pre-existing `ahc-hotspot` profile first: `nmcli device wifi
    hotspot` reuses a connection of the same con-name if one already exists, so a changed
    SSID/password on a repeat call would otherwise silently keep the OLD values instead of
    applying the new ones.
    """
    if not ssid.strip():
        return {"success": False, "message": "SSID cannot be empty"}
    if password and len(password) < 8:
        return {"success": False, "message": "Password must be at least 8 characters (WPA2), or empty for an open network"}
    try:
        _reject_unsafe_value(ssid, field="SSID", max_bytes=_MAX_SSID_BYTES)
        _reject_unsafe_value(password, field="Password", max_bytes=_MAX_PSK_LEN)
    except UnsafeWifiValue as exc:
        return {"success": False, "message": str(exc)}

    iface = await _find_wifi_interface()
    if iface is None:
        return {"success": False, "message": "This device has no WiFi radio"}

    # Found live 2026-07-19: on a board where Ethernet is active, the auto-disable-wifi policy
    # above has already turned the radio off (`nmcli device status` shows wlan0 as
    # "unavailable") -- `nmcli device wifi hotspot` fails with a confusing "device is not
    # available" in that state, distinct from the root-only-D-Bus error this function otherwise
    # routes around. Set the override first (before touching the radio) so the monitor loop
    # can't immediately re-disable it out from under the hotspot we're about to create.
    await set_user_wifi_override(True)
    await enable_wifi()

    # `enable_wifi()` returns as soon as `nmcli radio wifi on` exits, not once the device is
    # actually usable -- found live 2026-07-19, real device confirmed on the Cubie A5E: takes
    # ~2-3s to cycle unavailable -> disconnected -> connected. Without this wait, the very next
    # hotspot command below raced ahead of that and failed with "device is not available" on
    # every real attempt through the app (reproduced live via Paras's own admin session).
    if not await _wait_for_wifi_device_ready(iface):
        return {"success": False, "message": "WiFi radio did not become ready in time"}

    safe = _safe_profile_name(ssid)
    staged_path = _HOTSPOT_STAGING_DIR / f"ahc-hotspot-{safe}.conf"
    try:
        _HOTSPOT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(f"{iface}\n{ssid}\n{password}\n")
        staged_path.chmod(0o600)
    except OSError as exc:
        return {"success": False, "message": f"Failed to stage hotspot config: {exc}"}

    try:
        escaped_safe = await _systemd_escape(safe)
    except RuntimeError as exc:
        logger.warning("Failed to escape hotspot SSID for systemd: %s", exc)
        staged_path.unlink(missing_ok=True)
        return {"success": False, "message": "Failed to start hotspot"}

    rc, _, err = await run_command(
        ["systemctl", "start", f"ahc-hotspot-enable@{escaped_safe}.service"], timeout=30
    )
    if rc != 0:
        logger.warning("Failed to start hotspot: %s", err)
        staged_path.unlink(missing_ok=True)
        return {"success": False, "message": err or "Failed to start hotspot"}

    return {"success": True, "message": "Hotspot started"}


async def disable_hotspot() -> bool:
    """Stop the hotspot connection via the ahc-hotspot-disable host-namespace root helper (same
    root-only restriction as enable_hotspot -- `nmcli connection down` on a shared/AP-mode
    connection hits the same "Not authorized" error for the unprivileged aihomecloud user).
    Does NOT auto-reconnect to a saved client-mode network -- that's nmcli's own default
    behavior for `connection down`, not a policy decision made here."""
    rc, _, err = await run_command(["systemctl", "start", "ahc-hotspot-disable.service"], timeout=15)
    if rc != 0:
        logger.warning("Failed to stop hotspot: %s", err)
        return False
    return True
