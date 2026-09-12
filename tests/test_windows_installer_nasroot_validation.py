"""
Logic-parity tests for the Windows installer's NAS-root validation, runnable on macOS/Linux
where the real implementation (backend/install_windows.ps1's Test-SafeNasRoot,
backend/installer/AiHomeCloud.iss's ContainsUnsafeChar/PSSingleQuoteEscape) cannot execute --
there is no PowerShell or Inno Setup compiler in this environment, and the Windows test
machine is unavailable (see kb/handoff_windows_installer_2026-08-20.md).

These are NOT the production implementation and must NOT be imported by it. They are a
hand-synced mirror of the same *rules*, kept deliberately tiny (pure functions, no Windows API
calls) so a change to the real PowerShell/Pascal logic is easy to eyeball against this file and
update. If the two ever drift, this file is wrong, not a spec -- re-sync it against the real
functions rather than "fixing" the PowerShell to match a stale test.

What this proves, precisely:
  1. contains_unsafe_char() mirrors ContainsUnsafeChar (AiHomeCloud.iss) -- the characters
     Windows itself reserves in a path (< > " | ? * and control chars) are rejected outright,
     at BOTH call sites that now use it (interactive NextButtonClick, and the caller-independent
     CurStepChanged guard added in the 2026-08-21 verification pass).
  2. is_drive_relative() mirrors Test-SafeNasRoot's new upfront gate (2026-09-12 fix) -- a
     NasRoot that isn't already a fully-qualified, drive-rooted path (e.g. "D:\\AiHomeCloud") is
     refused outright, before any resolution happens, since a drive-relative form like "C:temp"
     resolves against that drive's own current directory (a per-process Windows concept no
     static check can predict).
  3. is_dangerous_nas_root() mirrors Test-SafeNasRoot's dangerous-path check
     (install_windows.ps1) -- bare drive roots and known Windows system/app directories are
     refused regardless of caller (GUI, silent /NASROOT=, or direct script invocation). As of the
     2026-09-12 fix it normalizes ".." / "." segments FIRST (mirroring
     [System.IO.Path]::GetFullPath()), so a lexically-innocuous traversal path that resolves to
     a dangerous directory (e.g. "C:\\Innocuous\\..\\..\\Windows" -> "C:\\Windows") is caught
     too, not just an already-dangerous literal string.
  4. ps_single_quote_escape() mirrors PSSingleQuoteEscape (AiHomeCloud.iss) -- and
     _parse_ps_single_quoted_arg() is an independent, from-scratch implementation of
     PowerShell's actual single-quoted-string grammar (open ' ... close ', with '' -> a
     literal ' and nothing else ever escaped). Round-tripping every test value through
     escape-then-parse and asserting the result equals the original input is the real proof
     that "no user-controlled NAS path can escape its intended PowerShell string context" --
     not merely that some blacklist of characters was rejected.
"""

from __future__ import annotations

import ntpath
import os
import re

import pytest

# ---------------------------------------------------------------------------------------------
# Mirror of ContainsUnsafeChar (backend/installer/AiHomeCloud.iss)
# ---------------------------------------------------------------------------------------------
_WINDOWS_RESERVED_CHARS = '<>"|?*'


def contains_unsafe_char(path: str) -> bool:
    if any(c in _WINDOWS_RESERVED_CHARS for c in path):
        return True
    return any(ord(c) < 32 for c in path)


# ---------------------------------------------------------------------------------------------
# Mirror of Test-SafeNasRoot's dangerous-path check (backend/install_windows.ps1)
# Fixed to the real installer's actual default environment variable targets rather than reading
# live env vars, so this test suite's result doesn't depend on the host it runs on.
# ---------------------------------------------------------------------------------------------
_DANGEROUS_PREFIXES = (
    r"C:\WINDOWS",
    r"C:\PROGRAM FILES",
    r"C:\PROGRAM FILES (X86)",
    r"C:\PROGRAMDATA",
    r"C:\USERS",
)

# Mirror of the upfront regex Test-SafeNasRoot now checks BEFORE any resolution: NasRoot must
# already read as a fully-qualified drive-rooted path ("D:\..."). PowerShell's -match is
# case-insensitive by default, hence re.IGNORECASE here.
_DRIVE_ROOTED_RE = re.compile(r"^[A-Za-z]:\\")


def is_drive_relative(path: str) -> bool:
    """Mirror of Test-SafeNasRoot's new upfront guard (2026-09-12): a path like "C:temp" (drive
    letter + colon, no backslash) is drive-relative in Windows -- it resolves against that
    drive's OWN current directory, not the drive root -- so it must be rejected outright rather
    than resolved and guessed at."""
    return not bool(_DRIVE_ROOTED_RE.match(path))


def is_dangerous_nas_root(path: str) -> bool:
    # Mirror of [System.IO.Path]::GetFullPath(): collapse ".."/"." segments BEFORE the
    # dangerous-prefix comparison, so a traversal path that only *resolves* to a dangerous
    # directory (rather than lexically starting with one) is still caught.
    resolved = ntpath.normpath(path)
    trimmed = resolved.rstrip("\\")
    if len(trimmed) <= 2 or trimmed[1] != ":":
        return True  # bare drive root ("D:", "D:\") or not a real drive-letter path at all
    upper = trimmed.upper()
    return any(upper == p or upper.startswith(p + "\\") for p in _DANGEROUS_PREFIXES)


def is_reparse_point_ancestor(path: str) -> bool:
    """Mirror of Test-SafeNasRoot's ancestor-walk reparse-point check. Uses os.path.islink()
    since this test runs on macOS/Linux, not NTFS junction detection (Get-Item.LinkType) --
    the underlying platform mechanism differs, but the LOGIC under test (walk every existing
    ancestor, refuse if any is a filesystem link) is identical, which is what this proves."""
    p = path.rstrip("/")
    while len(p) > 1:
        if os.path.exists(p) and os.path.islink(p):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return False


# ---------------------------------------------------------------------------------------------
# Mirror of PSSingleQuoteEscape (backend/installer/AiHomeCloud.iss)
# ---------------------------------------------------------------------------------------------
def ps_single_quote_escape(value: str) -> str:
    return value.replace("'", "''")


def _parse_ps_single_quoted_arg(fragment: str) -> str:
    """Independent, from-scratch parser for PowerShell's single-quoted string grammar, used
    only to verify round-tripping in tests -- deliberately NOT sharing any code with
    ps_single_quote_escape() above, so a bug can't cancel itself out between encoder and
    decoder. `fragment` must be exactly `'...'` (opening quote, content, closing quote) as it
    would appear embedded in real PowerShell source. Raises ValueError on malformed input
    (unterminated string, or trailing content after the closing quote) -- that would itself be
    a break-out, not a parse edge case to shrug off.
    """
    if not fragment.startswith("'"):
        raise ValueError("fragment must start with an opening single quote")
    i = 1
    out: list[str] = []
    n = len(fragment)
    while i < n:
        c = fragment[i]
        if c == "'":
            if i + 1 < n and fragment[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            # Lone closing quote -- the string ends here. Anything after this point is OUTSIDE
            # the intended single-quoted argument, in real PowerShell script-source terms.
            remainder = fragment[i + 1 :]
            if remainder != "":
                raise ValueError(
                    f"content after closing quote -- value escaped its intended PowerShell "
                    f"string context: {remainder!r}"
                )
            return "".join(out)
        out.append(c)
        i += 1
    raise ValueError("unterminated single-quoted string -- no closing quote found")


def build_nasroot_arg_fragment(raw_value: str) -> str:
    """Mirrors exactly what AiHomeCloud.iss's CurStepChanged builds:
    `-NasRoot '<escaped>'` -- reproduced here only to have something realistic to round-trip,
    not as a claim that the full Params string is being re-tested."""
    return "'" + ps_single_quote_escape(raw_value) + "'"


# ===============================================================================================
# Tests
# ===============================================================================================

VALID_PATHS = [
    r"D:\AiHomeCloud",
    r"D:\AiHomeCloud\Data",
    r"D:\Family Photos",
    r"D:\Dad's Photos",
    "D:\\家族写真",  # Unicode (Japanese "family photos")
    "D:\\Café Photos",  # Unicode (accented Latin)
    r"D:\AiHomeCloud\Media\2026\Photos\August",  # nested
    r"E:\Photos (Backup)",  # parentheses -- legal on Windows, must not be rejected
]

DANGEROUS_PATHS = [
    "C:\\",
    r"C:",
    "D:\\",
    r"C:\Windows",
    r"c:\windows\system32",  # case-insensitivity
    r"C:\Program Files",
    r"C:\Program Files\AiHomeCloud-Setup-Files",  # the installer's own staging dir
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\Users",
    r"C:\Users\SomeUser",
]

# The exact class of attack CodeRabbit's audit flagged: a lexically-innocuous path that
# resolves (via ".." collapsing) to a dangerous directory. Pre-fix, is_dangerous_nas_root did a
# raw string comparison and let every one of these through.
TRAVERSAL_ATTACK_PATHS = [
    r"C:\Innocuous\..\..\Windows",  # the report's own example
    r"C:\AiHomeCloud\..\..\Windows\System32",
    r"C:\Program Files\..\Windows",
    r"C:\Users\SomeUser\..\..\..\Windows",
    r"C:\Innocuous\..\..\windows\system32",  # traversal + case-insensitivity together
]

# Drive-relative paths (drive letter + colon, NOT followed by a backslash) -- these resolve
# against that drive's own current directory in real Windows, not the drive root, so the fix
# rejects them outright rather than attempting to resolve them.
DRIVE_RELATIVE_PATHS = [
    "C:temp",
    "C:AiHomeCloud",
    "D:Data",
    "C:..\\Windows",  # drive-relative AND a traversal attempt -- still caught by the same gate
]

# One representative payload per character the audit's injection brief called out, each
# embedded in an otherwise-ordinary-looking NAS path.
INJECTION_PAYLOADS = {
    "apostrophe": r"D:\Test'Path",
    "double_quote": 'D:\\Test"Path',
    "backtick": "D:\\Test`Path",
    "dollar": r"D:\Test$Path",
    "semicolon": r"D:\Test;Path",
    "pipe": r"D:\Test|Path",
    "ampersand": r"D:\Test&Path",
    "less_than": r"D:\Test<Path",
    "greater_than": r"D:\Test>Path",
}

# Characters Windows itself reserves in a path -- ContainsUnsafeChar rejects these outright,
# so they never reach PowerShell string construction at all in the fixed code.
_WINDOWS_ILLEGAL_PAYLOAD_KEYS = {"double_quote", "pipe", "less_than", "greater_than"}
# Characters that ARE legal in a real Windows path and therefore must be ALLOWED through --
# their safety comes entirely from PSSingleQuoteEscape's round-trip-safe escaping, not from
# being blocked.
_LEGAL_BUT_PS_META_PAYLOAD_KEYS = {"apostrophe", "backtick", "dollar", "semicolon", "ampersand"}


class TestContainsUnsafeChar:
    @pytest.mark.parametrize("path", VALID_PATHS)
    def test_legitimate_paths_are_not_flagged(self, path):
        assert contains_unsafe_char(path) is False

    @pytest.mark.parametrize(
        "key", sorted(_WINDOWS_ILLEGAL_PAYLOAD_KEYS)
    )
    def test_windows_illegal_characters_are_flagged(self, key):
        assert contains_unsafe_char(INJECTION_PAYLOADS[key]) is True

    @pytest.mark.parametrize(
        "key", sorted(_LEGAL_BUT_PS_META_PAYLOAD_KEYS)
    )
    def test_legal_but_powershell_meaningful_characters_are_not_flagged(self, key):
        # These must NOT be rejected here -- they are legal Windows path characters (a real
        # apostrophe'd family name is exactly the case the audit found being wrongly blocked
        # by the old blacklist). Their safety comes from escaping, tested separately below.
        assert contains_unsafe_char(INJECTION_PAYLOADS[key]) is False

    def test_control_characters_are_flagged(self):
        assert contains_unsafe_char("D:\\Test\x07Path") is True


class TestIsDangerousNasRoot:
    @pytest.mark.parametrize("path", VALID_PATHS)
    def test_legitimate_paths_are_not_flagged(self, path):
        assert is_dangerous_nas_root(path) is False

    @pytest.mark.parametrize("path", DANGEROUS_PATHS)
    def test_dangerous_paths_are_flagged(self, path):
        assert is_dangerous_nas_root(path) is True

    @pytest.mark.parametrize("path", TRAVERSAL_ATTACK_PATHS)
    def test_traversal_paths_resolving_to_a_dangerous_directory_are_flagged(self, path):
        """The 2026-09-12 fix: pre-fix, this was a raw string comparison against the
        unresolved NasRoot, so "C:\\Innocuous\\..\\..\\Windows" never lexically starts with
        "C:\\WINDOWS" and sailed through -- handing the low-privilege service account write
        access to C:\\Windows. Post-fix, ".."/"." segments are collapsed (mirroring
        [System.IO.Path]::GetFullPath()) BEFORE the prefix comparison, so the RESOLVED
        destination is what gets checked."""
        assert is_dangerous_nas_root(path) is True

    def test_traversal_paths_resolve_to_exactly_the_expected_dangerous_directory(self):
        """Not just "flagged somehow" -- prove the resolution itself is correct, i.e. this
        genuinely lands on the same directory the raw dangerous-path check already refuses."""
        assert ntpath.normpath(r"C:\Innocuous\..\..\Windows") == r"C:\Windows"

    def test_traversal_to_a_safe_directory_is_still_allowed(self):
        """Negative control: traversal by itself isn't the crime -- resolving into a genuinely
        safe location must still be permitted, proving the fix checks the RESOLVED destination
        rather than blanket-rejecting every ".." (which would break legitimate nested-then-back
        paths like installer-generated defaults)."""
        assert is_dangerous_nas_root(r"D:\AiHomeCloud\Media\..\Data") is False


class TestIsDriveRelative:
    @pytest.mark.parametrize("path", VALID_PATHS)
    def test_legitimate_paths_are_not_flagged(self, path):
        assert is_drive_relative(path) is False

    @pytest.mark.parametrize("path", DRIVE_RELATIVE_PATHS)
    def test_drive_relative_paths_are_flagged(self, path):
        """These must be rejected outright by the upfront gate -- their real target depends on
        that drive's current directory, which cannot be predicted or safely resolved here."""
        assert is_drive_relative(path) is True

    @pytest.mark.parametrize("path", DANGEROUS_PATHS + TRAVERSAL_ATTACK_PATHS)
    def test_already_rooted_dangerous_and_traversal_paths_are_not_caught_by_this_gate(self, path):
        """This gate only rejects the drive-relative FORM. Paths that are already
        fully-qualified (even dangerous or traversal-laden ones) must pass it through to the
        next gate (is_dangerous_nas_root) rather than being misclassified here -- each gate has
        exactly one job."""
        if path in ("C:", "D:"):
            pytest.skip("bare 'C:' with no backslash at all is itself drive-relative")
        assert is_drive_relative(path) is False


class TestSafeNasRootCombinedGateOrder:
    """End-to-end mirror of Test-SafeNasRoot's actual call order: the drive-relative gate runs
    FIRST on the raw input, then normalization + the dangerous-directory check. Every path in
    this module's classification lists must land in exactly one bucket -- there is no path that
    should silently pass both gates undetected other than a genuinely safe one."""

    def _passes_safe_nas_root(self, path: str) -> bool:
        if is_drive_relative(path):
            return False
        return not is_dangerous_nas_root(path)

    @pytest.mark.parametrize("path", VALID_PATHS)
    def test_valid_paths_pass_the_full_gate(self, path):
        assert self._passes_safe_nas_root(path) is True

    @pytest.mark.parametrize("path", DANGEROUS_PATHS)
    def test_dangerous_paths_are_rejected_by_the_full_gate(self, path):
        assert self._passes_safe_nas_root(path) is False

    @pytest.mark.parametrize("path", TRAVERSAL_ATTACK_PATHS)
    def test_traversal_paths_are_rejected_by_the_full_gate(self, path):
        assert self._passes_safe_nas_root(path) is False

    @pytest.mark.parametrize("path", DRIVE_RELATIVE_PATHS)
    def test_drive_relative_paths_are_rejected_by_the_full_gate(self, path):
        assert self._passes_safe_nas_root(path) is False


class TestReparsePointAncestor:
    def test_real_folder_is_not_flagged(self, tmp_path):
        real_dir = tmp_path / "AiHomeCloud"
        real_dir.mkdir()
        assert is_reparse_point_ancestor(str(real_dir)) is False

    def test_nonexistent_path_is_not_flagged(self, tmp_path):
        # Mirrors the real installer's behavior: a NAS root that doesn't exist yet (the normal
        # first-install case, since New-Directories creates it) must not be rejected -- there's
        # nothing to walk yet, so nothing can be a link.
        assert is_reparse_point_ancestor(str(tmp_path / "DoesNotExistYet" / "Data")) is False

    def test_symlinked_ancestor_is_flagged(self, tmp_path):
        real_target = tmp_path / "RealTarget"
        real_target.mkdir()
        linked = tmp_path / "LinkedFolder"
        os.symlink(real_target, linked)
        candidate = linked / "Data"
        assert is_reparse_point_ancestor(str(candidate)) is True

    def test_symlink_itself_is_flagged(self, tmp_path):
        real_target = tmp_path / "RealTarget"
        real_target.mkdir()
        linked = tmp_path / "LinkedFolder"
        os.symlink(real_target, linked)
        assert is_reparse_point_ancestor(str(linked)) is True


class TestPowerShellStringInjectionSafety:
    """The core property: after ps_single_quote_escape(), embedding the result inside a
    PowerShell single-quoted string literal and parsing that literal back with an independent
    parser must reproduce the ORIGINAL input exactly -- proving the value never escaped its
    intended string context, for every character the audit's injection brief named."""

    @pytest.mark.parametrize("path", VALID_PATHS)
    def test_valid_paths_round_trip_exactly(self, path):
        fragment = build_nasroot_arg_fragment(path)
        assert _parse_ps_single_quoted_arg(fragment) == path

    @pytest.mark.parametrize(
        "key", sorted(_LEGAL_BUT_PS_META_PAYLOAD_KEYS)
    )
    def test_legal_but_powershell_meaningful_payloads_round_trip_exactly(self, key):
        payload = INJECTION_PAYLOADS[key]
        fragment = build_nasroot_arg_fragment(payload)
        assert _parse_ps_single_quoted_arg(fragment) == payload

    def test_all_injection_payloads_either_round_trip_or_are_rejected_upstream(self):
        """Whole-suite assertion matching the audit's own framing: for every payload the brief
        called out, EITHER it's a Windows-illegal character (rejected by contains_unsafe_char
        before ever reaching PowerShell -- both interactively and, since the 2026-08-21 fix, on
        the silent-install path in CurStepChanged), OR it's legal and must survive the
        escape/parse round trip unchanged. There is no third outcome."""
        for key, payload in INJECTION_PAYLOADS.items():
            if contains_unsafe_char(payload):
                assert key in _WINDOWS_ILLEGAL_PAYLOAD_KEYS, (
                    f"{key!r} was rejected by contains_unsafe_char but isn't in the "
                    f"expected illegal set -- test's own classification is stale"
                )
            else:
                assert key in _LEGAL_BUT_PS_META_PAYLOAD_KEYS, (
                    f"{key!r} was NOT rejected by contains_unsafe_char and isn't in the "
                    f"expected legal set -- classify it before trusting it round-trips safely"
                )
                fragment = build_nasroot_arg_fragment(payload)
                assert _parse_ps_single_quoted_arg(fragment) == payload

    def test_malicious_close_and_reopen_payload_is_neutralized(self):
        """The actual attack this whole mechanism defends against: a value engineered to close
        the PowerShell string early and inject a second command. Pre-fix (raw interpolation,
        no escaping at all) this would have terminated the string exactly where the comment
        says and let '; Remove-Item -Recurse -Force C:\\ -Confirm:$false #' execute as real
        PowerShell source. Post-fix, ps_single_quote_escape's doubling neutralizes it --
        parsing the escaped fragment back must yield the exact original malicious-looking
        STRING (proving it was never executed as code, only ever treated as inert text)."""
        payload = r"D:\x'; Remove-Item -Recurse -Force C:\ -Confirm:$false #"
        fragment = build_nasroot_arg_fragment(payload)
        # If the escape were missing/broken, this parse would either raise (content after an
        # early close) or silently truncate -- either way it would NOT equal the full payload.
        assert _parse_ps_single_quoted_arg(fragment) == payload

    def test_unescaped_apostrophe_would_have_broken_out(self):
        """Negative control proving the round-trip test itself is meaningful: skipping the
        escape step on the exact same malicious payload must be detectably unsafe (either a
        parse error, from an unterminated/malformed remainder, or -- the actually dangerous
        case -- a truncated value that silently drops content, which is exactly what "breaking
        out of the string" looks like from the parser's side)."""
        payload = r"D:\x'; Remove-Item -Recurse -Force C:\ -Confirm:$false #"
        unescaped_fragment = "'" + payload + "'"  # no ps_single_quote_escape() applied
        with pytest.raises(ValueError):
            _parse_ps_single_quoted_arg(unescaped_fragment)
