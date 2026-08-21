#Requires -RunAsAdministrator
<#
.SYNOPSIS
    AiHomeCloud — Windows Installer

.DESCRIPTION
    Windows counterpart to install.sh, following the same architecture: same backend/app,
    same requirements.txt, same config.py (data_dir/nas_root now platform-aware — see
    _default_data_dir()/_default_nas_root()). This script's job is platform integration
    only: directory layout, a Windows Service (via NSSM, not a hand-rolled Win32 service —
    see the architecture decision doc's reasoning), secret protection (DPAPI via
    windows_secrets.py), and a firewall rule. mDNS advertisement needs no separate install
    step here, unlike Linux's avahi — main.py's lifespan starts app/mdns_advertiser.py
    in-process automatically on Windows.

    UNTESTED ON REAL WINDOWS. Written 2026-08-19 with no Windows machine available to run
    it on. Every command here is used correctly per its own documented syntax (NSSM's CLI
    has been stable for years; DPAPI/netsh/Windows Service Control Manager likewise), but
    "correct against the docs" is not "verified running" — compile-check and dry-run this
    for real before it ships. Do not represent this as a finished, tested installer.

.NOTES
    Idempotent by intent, matching install.sh's own invariant — re-running should not
    duplicate the service, overwrite an already-generated secret, or destroy user data.
    That intent has NOT been exercised by a real second run, unlike install.sh's, which has
    years of real re-run history behind its idempotency claims. Treat this script's
    idempotency as a design goal, not a verified property, until it's actually been rerun
    on a real machine.
#>

param(
    [string]$InstallDir = "$env:ProgramFiles\AiHomeCloud",
    [string]$DataDir = "$env:ProgramData\AiHomeCloud",
    [string]$NasRoot = "C:\AiHomeCloud\Data",
    [int]$Port = 8443,
    [switch]$SkipFirewall,
    [switch]$AllowDomainJoined,
    # Opt-in only, matching the Android app's own Remote Access step -- installing a real
    # third-party VPN client under someone else's terms is a consent moment, not a default. The
    # GUI wizard has no dedicated consent page for this yet (tracked separately, see
    # kb/remote_access_monetization_pivot_plan.md P1 item 6), so today this only fires for
    # someone who explicitly passes the flag on the command line.
    [switch]$EnableRemoteAccess,
    # Opt-in, off by default -- matches the Linux boards' own actual setup (onnxruntime/
    # tokenizers and the model files are deliberately NOT in requirements.txt or install.sh
    # either; a fresh install should not pull ~35MB nobody asked for yet). See
    # kb/remote_access_monetization_pivot_plan.md P2 item 10.
    [switch]$EnableSemanticSearch
)

$ErrorActionPreference = "Stop"
$ServiceName = "AiHomeCloud"
$IssuerServiceName = "AiHomeCloudCertIssuer"
# Dedicated low-privilege account for the main (network-facing) service -- the Windows analog of
# Linux's dedicated aihomecloud:aihomecloud system user. Found 2026-08-20: without this, NSSM's
# default (LocalSystem) means the network-facing service already has more privilege than the
# entire cert-issuance separation below exists to withhold from it, making that separation
# theatre. $IssuerServiceName above stays LocalSystem deliberately -- it's the one component
# meant to be privileged, exactly mirroring root's role on Linux.
$ServiceAccountName = "aihomecloud"
$PythonPrefix = Join-Path $InstallDir "python312"
$VenvDir = Join-Path $InstallDir "venv"
$BackendSrc = Join-Path $InstallDir "backend"
$NssmDir = Join-Path $InstallDir "nssm"
$NssmExe = Join-Path $NssmDir "nssm.exe"

# python-build-standalone -- the SAME provisioning source install.sh already uses and has
# verified live (Tier 2 of provision_python() there). Reusing the identical release/version
# pins keeps both platforms on one verified Python build rather than two separately-sourced
# ones. See install.sh's own PYTHON_STANDALONE_RELEASE/PYTHON_STANDALONE_VERSION comments
# for why this exact release was chosen.
$PythonStandaloneRelease = "20260623"
$PythonStandaloneVersion = "3.12.13"
$PythonStandaloneUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PythonStandaloneRelease/cpython-$PythonStandaloneVersion+$PythonStandaloneRelease-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
# Computed directly from the exact bytes at $PythonStandaloneUrl, 2026-08-20 -- production audit
# (see kb/status.md) flagged that this download ran with no integrity check at all before a
# LocalSystem-adjacent service ever executes anything from it. Pinning to this specific release's
# hash means a compromised GitHub release or an on-path MITM gets a hard failure, not a silent
# swap. Re-derive with `shasum -a 256` (or `Get-FileHash -Algorithm SHA256`) any time either
# version constant above changes -- a version bump with a stale hash here is a self-inflicted DoS,
# not a security improvement, so it must be updated in the same commit as either constant.
$PythonStandaloneSha256 = "de3e362376859b060fa8b856c434efa81fcf6d4ede3d6e177c7e2169670cac50"

# NSSM (Non-Sucking Service Manager) -- wraps an arbitrary .exe as a real Windows service
# without writing a Win32 service class by hand. Chosen over a hand-rolled pywin32 service
# per the architecture decision doc's explicit "reuse before building" call: this is
# mature, widely-deployed tooling, not something worth re-implementing for v1.
$NssmVersion = "2.24"
$NssmUrl = "https://nssm.cc/release/nssm-$NssmVersion.zip"
# Same reasoning as $PythonStandaloneSha256 above -- nssm.cc is a single small third-party host
# serving a binary that gets registered as the manager of a LocalSystem service. Re-derive if
# $NssmVersion changes.
$NssmSha256 = "727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743"

# Tailscale (remote access, opt-in -- see Install-RemoteAccess below). Version and hash both
# verified live 2026-08-20 by downloading the real file from pkgs.tailscale.com and running
# `shasum -a 256` against it directly -- not copied from a changelog or invented. Re-derive both
# together the same way any time $TailscaleVersion changes, same reasoning as the Python/NSSM
# pins above.
$TailscaleVersion = "1.102.3"
$TailscaleMsiUrl = "https://pkgs.tailscale.com/stable/tailscale-setup-$TailscaleVersion-amd64.msi"
$TailscaleMsiSha256 = "03ac8183c6e3ce276e9b44281ebe7e4c02aef28a971034ca170c4b665df42dce"

# Semantic search (optional, ~35MB -- see Install-SemanticSearch below). Same model the Linux
# fleet actually runs (bge-small-en-v1.5, DEFAULT_MODEL in app/embedding.py) and the same exact
# source file the project's own spike script uses (docs/spikes/phase4_embedding_bench.py) --
# onnx/model_quantized.onnx, not one of the repo's other 6 ONNX variants. Both files and hashes
# verified live 2026-08-20 by downloading them for real from huggingface.co and hashing them,
# same discipline as the Python/NSSM/Tailscale pins above.
$SemanticModelName = "bge-small-en-v1.5"
$SemanticModelOnnxUrl = "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model_quantized.onnx"
$SemanticModelOnnxSha256 = "6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4"
$SemanticModelTokenizerUrl = "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/tokenizer.json"
$SemanticModelTokenizerSha256 = "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"

function Write-Step($msg) { Write-Host "[AiHomeCloud] $msg" -ForegroundColor Green }
function Write-WarnStep($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

function Test-CommandExists($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# Verifies a downloaded file against its pinned SHA-256 before anything reads it further. Throws
# (caught by the top-level try/catch, aborting the install) rather than warning and continuing --
# a hash mismatch means either a corrupted download or a substituted file, and either way this is
# about to be extracted and, for NSSM, registered as the manager of a LocalSystem service.
function Assert-FileHash($path, $expectedSha256, $whatFor) {
    $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash
    if ($actual -ne $expectedSha256) {
        throw "$whatFor failed integrity check: expected SHA-256 $expectedSha256, got $actual. " +
            "This could mean a corrupted download or a tampered file -- not safe to continue."
    }
}

# ── 0. Domain-joined check ──────────────────────────────────────────────────
# AiHomeCloud is designed for a personal/home machine the installing user fully controls --
# a domain-joined machine is centrally managed by someone else's IT department, who did not
# consent to a new low-privilege account, two new services, and a firewall rule appearing on
# it. Refuse by default rather than silently doing all of that on a corporate asset; -AllowDomainJoined
# is the explicit opt-out for anyone who has actually thought about it (e.g. a personal laptop
# that happens to be domain-joined for an unrelated reason).
function Test-NotDomainJoined {
    Write-Step "[0/9] Checking this isn't a domain-managed machine..."
    $cs = Get-CimInstance -ClassName Win32_ComputerSystem
    if ($cs.PartOfDomain -and -not $AllowDomainJoined) {
        throw "This machine is joined to the '$($cs.Domain)' domain, which usually means it's " +
            "centrally managed by an IT department -- not a personal device this installer should " +
            "modify without that department's knowledge (new local account, two new Windows " +
            "services, a firewall rule). If this is actually your own device and you understand " +
            "the tradeoff, re-run with -AllowDomainJoined."
    }
    Write-Step "  OK (not domain-joined, or -AllowDomainJoined passed)."
}

# ── 0b. NAS root safety check ────────────────────────────────────────────────
# The ONLY validation guaranteed to run regardless of caller. The Inno Setup wizard's own
# IsDangerousNasRoot/ContainsUnsafeChar (installer/AiHomeCloud.iss) give a friendly, fast
# interactive error -- but NextButtonClick (where that validation lives) never runs for a
# silent /NASROOT= install, since /VERYSILENT skips the wizard page entirely, and it never runs
# at all for anyone invoking this script directly. Audit 2026-08-21 found this meant a silent
# install or a direct script call with a dangerous -NasRoot (e.g. "C:\Windows") got ZERO
# protection -- the low-privilege service account would receive recursive Modify on it. This
# function is the real, structural gate: it runs here, every time, for every caller, so the
# Inno-side checks are now a UX nicety on top of this, not the only line of defense.
function Test-SafeNasRoot {
    Write-Step "[0b/9] Validating storage location..."
    $trimmed = $NasRoot.TrimEnd('\')
    if ($trimmed.Length -le 2 -or $trimmed[1] -ne ':') {
        throw "-NasRoot '$NasRoot' must be a full path starting with a drive letter, and not a bare drive root (e.g. D:\AiHomeCloud\Data, not D:\ or D:)."
    }
    $dangerous = @(
        $env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData,
        (Join-Path $env:SystemDrive "Users")
    ) | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\').ToUpperInvariant() }
    $upperTrimmed = $trimmed.ToUpperInvariant()
    foreach ($d in $dangerous) {
        if ($upperTrimmed -eq $d -or $upperTrimmed.StartsWith("$d\")) {
            throw "-NasRoot '$NasRoot' resolves under a Windows system/application directory -- refusing to grant the low-privilege service account recursive write access there. Choose a dedicated folder outside any system directory, e.g. D:\AiHomeCloud\Data."
        }
    }
    # Reparse-point check: a string-only dangerous-path comparison can be bypassed by pointing
    # an innocuous-looking folder at a protected location via a junction/symlink. A legitimate
    # media library should never itself (or via an ancestor) be a reparse point -- refuse rather
    # than silently following one to wherever it actually leads.
    for ($p = $trimmed; $p.Length -gt 3; $p = Split-Path $p -Parent) {
        if (Test-Path -LiteralPath $p) {
            $item = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
            if ($item -and $item.LinkType) {
                throw "-NasRoot '$NasRoot' contains a reparse point/junction/symlink at '$p' -- refusing, since its real target can't be verified safe. Choose a real, non-linked folder."
            }
        }
    }
    Write-Step "  OK ($NasRoot)."
}

# ── 1. Directories ────────────────────────────────────────────────────────────
# Three-way split, same invariant as Linux's data_dir/nas_root/binaries: uninstalling the
# app must never risk the user's files. InstallDir = binaries (Program Files convention —
# read-only after install, no user data ever written here). DataDir = app state, mirrors
# config.py's _default_data_dir() default exactly (ProgramData\AiHomeCloud) so a
# zero-config service start already agrees with what this script prepared. NasRoot = user
# media, deliberately NOT nested under either of the above.
function New-Directories {
    Write-Step "[1/9] Creating directories..."
    # $DataDir\logs was missing here -- NSSM's AppStdout/AppStderr don't create their own
    # parent directory, so the service ran with nowhere to write output and left zero trace
    # of why it wasn't healthy. Found 2026-08-20 on the first real run.
    foreach ($d in @($InstallDir, $DataDir, $NasRoot, "$DataDir\tls", "$DataDir\identity", "$DataDir\logs")) {
        if (-not (Test-Path $d)) {
            New-Item -ItemType Directory -Path $d -Force | Out-Null
            Write-Step "  Created: $d"
        }
    }
}

# ── 1b. Service account (privilege separation) ─────────────────────────────────
function New-ServiceAccount {
    Write-Step "[1b/9] Setting up dedicated low-privilege service account..."
    $existing = Get-LocalUser -Name $ServiceAccountName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Step "  Account '$ServiceAccountName' already exists."
    } else {
        # Any password satisfying complexity requirements works here -- Install-Service (below)
        # always resets it to a fresh one right before configuring the service's logon account,
        # so what's set here never actually gets used. Not persisted anywhere.
        $bytes = New-Object byte[] 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $initialPassword = [Convert]::ToBase64String($bytes) + "!Aa1"
        New-LocalUser -Name $ServiceAccountName `
            -Password (ConvertTo-SecureString $initialPassword -AsPlainText -Force) `
            -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires `
            -Description "AiHomeCloud backend service (low-privilege)" `
            | Out-Null
        # Deliberately no group membership beyond the implicit default -- this account should
        # have no more than what Grant-ServiceAccountAccess below and ChangeServiceConfig's
        # automatic "Log on as a service" grant (happens when nssm set ObjectName runs) provide.
        Write-Step "  Created local account '$ServiceAccountName'."
    }

    # Hide from the Windows logon screen -- a service account has no reason to ever appear there,
    # and a family member seeing an unexplained account they didn't create is legitimate, real
    # confusion (production audit 2026-08-20, kb/status.md). This is display-only, not a security
    # boundary: the account still can't do anything it couldn't before. Idempotent (re-running is
    # a no-op if the value is already set); applied unconditionally, not just in the branch above,
    # so an existing install picks this up on its next reinstall/repair run too.
    $specialAccountsKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList"
    New-Item -Path $specialAccountsKey -Force | Out-Null
    New-ItemProperty -Path $specialAccountsKey -Name $ServiceAccountName -Value 0 -PropertyType DWord -Force | Out-Null
    Write-Step "  Hidden from the Windows logon screen."

    Write-Step "  Granting directory access..."
    # InstallDir: Read & Execute only -- binaries, matching the "read-only after install" intent
    # already documented on New-Directories above. DataDir/NasRoot: Modify -- real read-write
    # needs for normal app operation. The specific identity/TLS files under DataDir get their
    # own stricter explicit ACLs later, set by windows_cert_issuer.py/windows_identity.py right
    # after they create each file (can't be done here -- those files don't exist yet at install
    # time). (OI)(CI) = apply to files and subfolders created later, not just what exists now.
    & icacls $InstallDir /grant:r "${ServiceAccountName}:(OI)(CI)RX" | Out-Null
    & icacls $DataDir /grant:r "${ServiceAccountName}:(OI)(CI)M" | Out-Null
    & icacls $NasRoot /grant:r "${ServiceAccountName}:(OI)(CI)M" | Out-Null
    Write-Step "  Access granted on InstallDir (read+execute), DataDir/NasRoot (modify)."

    # Security review 2026-08-20 (confirmed live via icacls on this exact machine, not
    # theoretical): identity\ and tls\ already exist by this point (New-Directories runs
    # first), so the recursive $DataDir grant above inherits onto them immediately -- the
    # low-privilege service account briefly ends up with Modify on identity\, meaning any file
    # windows_identity.py later creates there (the board's permanent Ed25519 signing key)
    # inherits that grant at the instant of creation, before the per-file lockdown in
    # windows_identity.py/windows_acl.py ever runs. Re-tighten both right here, before install
    # ever proceeds to a point where either service could write into them -- mirrors
    # ahc-generate-identity.sh's chmod 711-before-keygen ordering. (windows_identity.py and
    # windows_cert_issuer.py also lock their own directories immediately before writing, as a
    # second, independent close of the same gap -- this script-level fix does not depend on
    # that Python-side fix landing correctly, and vice versa.)
    Write-Step "  Locking down identity/ and tls/ subdirectories (H-11 privilege separation)..."
    & icacls "$DataDir\identity" /inheritance:r | Out-Null
    & icacls "$DataDir\identity" /grant:r "SYSTEM:(OI)(CI)F" | Out-Null
    & icacls "$DataDir\identity" /grant:r "Administrators:(OI)(CI)F" | Out-Null
    & icacls "$DataDir\tls" /inheritance:r | Out-Null
    & icacls "$DataDir\tls" /grant:r "SYSTEM:(OI)(CI)F" | Out-Null
    & icacls "$DataDir\tls" /grant:r "Administrators:(OI)(CI)F" | Out-Null
    & icacls "$DataDir\tls" /grant:r "${ServiceAccountName}:(OI)(CI)RX" | Out-Null
    Write-Step "  identity/ locked to SYSTEM/Administrators only; tls/ locked to SYSTEM/Administrators (full) + service account (read-only)."
}

# ── 1c. Stop any running services before redeploying code ──────────────────────
# Found 2026-08-20 on the first re-run: Copy-BackendCode's Remove-Item on $BackendSrc fails
# with "used by another process" once services exist and are running from it -- Windows won't
# delete a directory a running process has open, unlike install.sh's equivalent on Linux which
# doesn't hit this (systemctl stop happens implicitly as part of that flow). Harmless no-op on a
# fresh install where neither service exists yet.
function Stop-ExistingServices {
    Stop-Service $ServiceName, $IssuerServiceName -Force -ErrorAction SilentlyContinue
}

# ── 2. Python ─────────────────────────────────────────────────────────────────
function Install-Python {
    Write-Step "[2/9] Provisioning Python $PythonStandaloneVersion..."
    if (Test-Path "$PythonPrefix\python.exe") {
        Write-Step "  Python already provisioned at $PythonPrefix."
        return
    }
    $tmpArchive = Join-Path $env:TEMP "ahc_python_standalone.tar.gz"
    Write-Step "  Downloading prebuilt Python from python-build-standalone..."
    Invoke-WebRequest -Uri $PythonStandaloneUrl -OutFile $tmpArchive -UseBasicParsing
    Assert-FileHash $tmpArchive $PythonStandaloneSha256 "Python standalone runtime"
    Write-Step "  Integrity verified (SHA-256 match)."

    New-Item -ItemType Directory -Path $PythonPrefix -Force | Out-Null
    # tar ships in-box on Windows 10 1803+ / Server 2019+; this project has no lower target.
    tar -xzf $tmpArchive -C $PythonPrefix --strip-components=1
    Remove-Item $tmpArchive -Force

    $smokeTest = & "$PythonPrefix\python.exe" -c "import ssl, sqlite3, zlib, ctypes; print('OK')" 2>&1
    if ($smokeTest -notmatch "OK") {
        throw "Provisioned Python failed the import smoke test: $smokeTest"
    }
    Write-Step "  Python $PythonStandaloneVersion installed at $PythonPrefix — OK."
}

# ── 3. Deploy backend code ────────────────────────────────────────────────────
function Copy-BackendCode {
    Write-Step "[3/9] Deploying backend code..."
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $sourceBackend = Join-Path $repoRoot "backend"
    if (-not (Test-Path "$sourceBackend\app\main.py")) {
        # Fall back to "this script's own directory is the backend checkout" for the
        # standalone-export case (aihomecloud-server), matching install.sh's equivalent
        # script-relative-vs-parent-relative detection.
        $sourceBackend = $PSScriptRoot
    }
    if (-not (Test-Path "$sourceBackend\app\main.py")) {
        throw "Cannot find backend/app/main.py relative to this script."
    }
    if (Test-Path $BackendSrc) { Remove-Item $BackendSrc -Recurse -Force }
    Copy-Item $sourceBackend $BackendSrc -Recurse
    Write-Step "  Copied backend -> $BackendSrc"
}

# ── 4. Virtual environment ────────────────────────────────────────────────────
function New-Venv {
    Write-Step "[4/9] Setting up virtual environment..."
    if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
        & "$PythonPrefix\python.exe" -m venv $VenvDir
    }
    & "$VenvDir\Scripts\python.exe" -m pip install --quiet --upgrade pip
    & "$VenvDir\Scripts\pip.exe" install --quiet -r "$BackendSrc\requirements.txt"
    Write-Step "  Dependencies installed."
}

# ── 4b. Semantic search (optional) ──────────────────────────────────────────────
# Mirrors what the Linux fleet actually has, deliberately opt-in on both platforms: the runtime
# (onnxruntime + tokenizers, ~200MB) and the model files stay out of the default install so a
# fresh setup doesn't pull weight nobody asked for. Backend behavior on a board without this is
# already graceful -- app/embedding.py's available() returns False and /search/semantic 501s
# rather than erroring -- so this step being skipped is a supported, not broken, state.
#
# NOT YET LIVE-VERIFIED as of 2026-08-20 -- the model/tokenizer download URLs and hashes are
# real (verified live, see their own comment above), but `pip install` succeeding inside this
# venv and the resulting files actually loading via onnxruntime has not been run on a real
# Windows machine yet.
function Install-SemanticSearch {
    if (-not $EnableSemanticSearch) {
        Write-Step "[4b/9] Semantic search -- skipped (opt-in only; pass -EnableSemanticSearch to install it)."
        return
    }
    Write-Step "[4b/9] Setting up semantic search ($SemanticModelName)..."
    $modelDir = Join-Path $DataDir "models\$SemanticModelName"
    $modelOnnx = Join-Path $modelDir "model.onnx"
    $tokenizerJson = Join-Path $modelDir "tokenizer.json"

    if ((Test-Path $modelOnnx) -and (Test-Path $tokenizerJson)) {
        Write-Step "  Model files already present at $modelDir."
    } else {
        New-Item -ItemType Directory -Path $modelDir -Force | Out-Null
        Write-Step "  Downloading $SemanticModelName (~35MB)..."
        Invoke-WebRequest -Uri $SemanticModelOnnxUrl -OutFile $modelOnnx -UseBasicParsing
        Assert-FileHash $modelOnnx $SemanticModelOnnxSha256 "Semantic search model"
        Invoke-WebRequest -Uri $SemanticModelTokenizerUrl -OutFile $tokenizerJson -UseBasicParsing
        Assert-FileHash $tokenizerJson $SemanticModelTokenizerSha256 "Semantic search tokenizer"
        Write-Step "  Integrity verified (SHA-256 match) for both files."
    }

    Write-Step "  Installing onnxruntime + tokenizers into the venv..."
    & "$VenvDir\Scripts\pip.exe" install --quiet onnxruntime tokenizers
    $smokeTest = & "$VenvDir\Scripts\python.exe" -c "import onnxruntime, tokenizers; print('OK')" 2>&1
    if ($smokeTest -notmatch "OK") {
        Write-WarnStep "  onnxruntime/tokenizers import smoke test failed: $smokeTest"
        Write-WarnStep "  Semantic search will stay unavailable (embedding.available() will report False) -- the NAS itself is unaffected, this is optional."
        return
    }
    Write-Step "  Semantic search ready."
}

# ── 5. Secrets (DPAPI-protected, via windows_secrets.py) ─────────────────────
function New-Secrets {
    Write-Step "[5/9] Generating device identity and secrets..."
    # Delegates to the actual app code (app/windows_secrets.py) rather than reimplementing
    # DPAPI calls in PowerShell — one implementation of "how a secret is protected", used
    # by both the installer and the running service, not two that could drift apart.
    $genScript = @"
import secrets, sys
sys.path.insert(0, r'$BackendSrc')
from pathlib import Path
from app.windows_secrets import protect_secret_to_file, read_protected_secret

jwt_path = Path(r'$DataDir') / 'jwt_secret.dpapi'
pairing_path = Path(r'$DataDir') / 'pairing_key.dpapi'

if read_protected_secret(jwt_path) is None:
    protect_secret_to_file(secrets.token_hex(32), jwt_path, 'AiHomeCloud JWT secret')
    print('Generated JWT secret')
else:
    print('JWT secret already present')

if read_protected_secret(pairing_path) is None:
    protect_secret_to_file(secrets.token_urlsafe(16), pairing_path, 'AiHomeCloud pairing key')
    print('Generated pairing key')
else:
    print('Pairing key already present')
"@
    $genScript | & "$VenvDir\Scripts\python.exe" -
    if ($LASTEXITCODE -ne 0) {
        throw "Secret generation failed (see traceback above) -- DPAPI protection did not succeed. Not safe to continue."
    }
    Write-Step "  Secrets ready (DPAPI-protected, machine-scoped)."
    Write-WarnStep "  Moving to a new PC? These secrets do NOT copy — use the export/import"
    Write-WarnStep "  functions in app/windows_secrets.py (passphrase-based) before wiping this machine."
}

# ── 6. NSSM + Windows Service ─────────────────────────────────────────────────
function Install-Service {
    Write-Step "[6/9] Installing Windows service via NSSM..."

    if (-not (Test-Path $NssmExe)) {
        $tmpZip = Join-Path $env:TEMP "nssm.zip"
        Invoke-WebRequest -Uri $NssmUrl -OutFile $tmpZip -UseBasicParsing
        Assert-FileHash $tmpZip $NssmSha256 "NSSM"
        Write-Step "  Integrity verified (SHA-256 match)."
        $tmpExtract = Join-Path $env:TEMP "nssm_extract"
        Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force
        New-Item -ItemType Directory -Path $NssmDir -Force | Out-Null
        # 2.24's zip layout is nssm-2.24\win64\nssm.exe (and win32\ for 32-bit) -- picking
        # win64 unconditionally, matching this script's x86_64-only Python provisioning above.
        Copy-Item "$tmpExtract\nssm-$NssmVersion\win64\nssm.exe" $NssmExe
        Remove-Item $tmpZip, $tmpExtract -Recurse -Force
    }

    # Any 2> redirect on a native command (2>$null, 2>&1 -- the target doesn't matter) makes
    # PowerShell wrap that command's stderr lines as ErrorRecord objects. With
    # $ErrorActionPreference = "Stop" (set above), that then aborts the whole script -- even
    # for an expected, exit-code-handled failure like "service doesn't exist yet" on a first
    # install, which is exactly what nssm prints to stderr here. Fix: relax EAP only around
    # this one probe, restore it right after. Verified live: without this, the script died
    # here on every first-run install before $LASTEXITCODE was ever checked.
    $priorEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $NssmExe status $ServiceName 2>$null | Out-Null
    $statusExitCode = $LASTEXITCODE
    $ErrorActionPreference = $priorEAP

    if ($statusExitCode -eq 0) {
        Write-Step "  Service '$ServiceName' already installed — updating config, not recreating."
        $ErrorActionPreference = "Continue"
        & $NssmExe stop $ServiceName 2>$null | Out-Null
        $ErrorActionPreference = $priorEAP
    } else {
        & $NssmExe install $ServiceName "$VenvDir\Scripts\python.exe" "-m app.main"
    }

    # Privilege separation: run as the dedicated low-privilege account (New-ServiceAccount
    # above), never LocalSystem -- see the top-of-file comment on $ServiceAccountName for why.
    # Always reset the password here rather than trying to remember one from account-creation
    # time (simpler, fully idempotent regardless of whether the account or the service is the
    # fresh one this run). ChangeServiceConfig grants "Log on as a service" to the account
    # automatically as part of this call -- verified live, not assumed.
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $svcPassword = [Convert]::ToBase64String($bytes) + "!Aa1"
    Set-LocalUser -Name $ServiceAccountName -Password (ConvertTo-SecureString $svcPassword -AsPlainText -Force)
    & $NssmExe set $ServiceName ObjectName ".\$ServiceAccountName" $svcPassword

    & $NssmExe set $ServiceName AppDirectory $BackendSrc
    & $NssmExe set $ServiceName AppEnvironmentExtra `
        "AHC_DATA_DIR=$DataDir" `
        "AHC_NAS_ROOT=$NasRoot" `
        "AHC_PLATFORM=windows" `
        "AHC_PORT=$Port"
    & $NssmExe set $ServiceName Start SERVICE_AUTO_START
    & $NssmExe set $ServiceName AppStdout (Join-Path $DataDir "logs\service.out.log")
    & $NssmExe set $ServiceName AppStderr (Join-Path $DataDir "logs\service.err.log")
    & $NssmExe set $ServiceName AppRotateFiles 1
    & $NssmExe set $ServiceName AppRotateOnline 1
    & $NssmExe set $ServiceName AppRotateBytes 10485760   # 10 MB

    Write-Step "  Service '$ServiceName' configured."
}

# ── 6c. Allow standard users to start/stop the service (tray helper) ───────────
# Only Administrators can start/stop a Windows service by default -- but app/windows_tray.py
# (the system-tray helper, registered by Install-TrayAutostart below) runs unelevated, as
# whichever family member is signed in, exactly like Teams' or Telegram's own tray app. Grants
# ONLY service lifecycle control (start/stop/query-status) to the Authenticated Users group on
# this ONE service object -- no file, TLS, or identity ACL changes anywhere, and
# AiHomeCloudCertIssuer's own ACL is untouched (stays Administrators/SYSTEM-only). This is a
# deliberate, narrow privilege widening, not an oversight: any locally signed-in account can now
# stop the NAS backend, which they could already do less cleanly with local access anyway
# (unplugging the machine, etc.) -- documented explicitly here per the tray-feature review
# (2026-08-21), not assumed silently safe.
function Grant-ServiceControlToUsers {
    Write-Step "[6c/9] Allowing signed-in users to start/stop the service from the tray..."
    $newAce = "(A;;RPWPLC;;;AU)"   # RP=start, WP=stop, LC=query-status; AU=Authenticated Users
    $currentSd = ((& sc.exe sdshow $ServiceName) -join "").Trim()
    if ($currentSd -match [regex]::Escape($newAce)) {
        Write-Step "  Already granted."
        return
    }
    # Insert before any SACL ("S:...") section rather than blindly appending at the very end --
    # a fresh NSSM-created service has no SACL, so this is equivalent in practice, but stays
    # correct if that ever changes.
    if ($currentSd -match '^(D:[A-Z]*)((?:\([^)]*\))*)(S:.*)?$') {
        $newSd = $Matches[1] + $Matches[2] + $newAce + $Matches[3]
    } else {
        $newSd = $currentSd + $newAce
    }
    & sc.exe sdset $ServiceName $newSd | Out-Null
    Write-Step "  Granted Start/Stop/Query rights to Authenticated Users on '$ServiceName'."
}

# ── 6d. Tray helper autostart ───────────────────────────────────────────────────
function Install-TrayAutostart {
    Write-Step "[6d/9] Registering the tray icon to start at sign-in..."
    # All-Users Startup folder, not a per-user HKCU Run key or the installing user's own Startup
    # folder: this script always runs elevated, and whether that elevated process's own per-user
    # context matches whoever's actually signed in depends on exactly how they elevated -- not
    # safe to assume. All-Users Startup applies to every account that signs into this machine,
    # matching the real intent (a shared family NAS, not a single-user workstation). A .lnk
    # rather than a raw Run-key string specifically because it carries its own WorkingDirectory --
    # `python -m app.windows_tray` needs to run from $BackendSrc for the `app` package to
    # resolve, and a plain Run-key command has no working-directory field at all.
    $startupDir = [Environment]::GetFolderPath("CommonStartup")
    $shortcutPath = Join-Path $startupDir "AiHomeCloud Tray.lnk"
    $pythonwExe = Join-Path $VenvDir "Scripts\pythonw.exe"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonwExe
    $shortcut.Arguments = "-m app.windows_tray"
    $shortcut.WorkingDirectory = $BackendSrc
    $shortcut.Description = "AiHomeCloud system tray"
    $shortcut.Save()
    # Persisted machine-wide so the tray (which starts fresh at each sign-in, with none of the
    # NSSM service's own AppEnvironmentExtra) knows the real port if -Port ever overrides the
    # default -- a system env var only takes effect for processes started after it's set, which
    # naturally matches "the tray only starts at next sign-in" anyway.
    [Environment]::SetEnvironmentVariable("AHC_PORT", $Port, "Machine")
    Write-Step "  Registered ($shortcutPath). The tray icon appears after the next sign-in."
}

# ── 6b. Cert issuer service (H-11 privilege separation, Windows side) ──────────
# Stands in for ahc-issue-cert.path/.sh -- the one component allowed to be privileged, mirroring
# root's role on Linux. Deliberately LEFT on LocalSystem (NSSM's default): this is the component
# that must be able to write identity.key and the ACLs on it, so it needs real privilege on
# purpose, unlike $ServiceName above.
function Install-CertIssuerService {
    Write-Step "[6b/9] Installing cert issuer service..."

    $priorEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $NssmExe status $IssuerServiceName 2>$null | Out-Null
    $statusExitCode = $LASTEXITCODE
    $ErrorActionPreference = $priorEAP

    if ($statusExitCode -eq 0) {
        Write-Step "  Service '$IssuerServiceName' already installed — updating config, not recreating."
        $ErrorActionPreference = "Continue"
        & $NssmExe stop $IssuerServiceName 2>$null | Out-Null
        $ErrorActionPreference = $priorEAP
    } else {
        & $NssmExe install $IssuerServiceName "$VenvDir\Scripts\python.exe" "-m app.windows_cert_issuer `"$DataDir`""
    }

    & $NssmExe set $IssuerServiceName AppDirectory $BackendSrc
    # AHC_SERVICE_ACCOUNT: how windows_identity.py / windows_cert_issuer.py know which account
    # to grant read (never write) access to when they set ACLs on identity.pub, tls/cert.pem,
    # tls/key.pem, and statement.json right after publishing each one.
    & $NssmExe set $IssuerServiceName AppEnvironmentExtra "AHC_SERVICE_ACCOUNT=$ServiceAccountName"
    & $NssmExe set $IssuerServiceName Start SERVICE_AUTO_START
    & $NssmExe set $IssuerServiceName AppStdout (Join-Path $DataDir "logs\issuer.out.log")
    & $NssmExe set $IssuerServiceName AppStderr (Join-Path $DataDir "logs\issuer.err.log")
    & $NssmExe set $IssuerServiceName AppRotateFiles 1
    & $NssmExe set $IssuerServiceName AppRotateOnline 1
    & $NssmExe set $IssuerServiceName AppRotateBytes 10485760   # 10 MB

    & $NssmExe start $IssuerServiceName
    Write-Step "  Service '$IssuerServiceName' configured and started."
}

# ── 7. Firewall ────────────────────────────────────────────────────────────────
function New-FirewallRule {
    if ($SkipFirewall) {
        Write-WarnStep "[7/9] Skipping firewall rule (-SkipFirewall passed)."
        return
    }
    Write-Step "[7/9] Configuring Windows Firewall..."
    $ruleName = "AiHomeCloud ($Port/tcp)"
    $existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Step "  Firewall rule already present."
        return
    }
    # Private/Domain profiles only, deliberately -- same default-conservative posture as the
    # architecture doc's networking guidance (avoid unnecessary public-internet exposure).
    # A user on a Public-profile network (coffee shop Wi-Fi, etc.) gets no inbound access,
    # which is the correct default for a home NAS regardless of "Public" being a mislabel
    # for e.g. a phone hotspot the user actually trusts -- they can widen this manually.
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
        -LocalPort $Port -Action Allow -Profile Private, Domain | Out-Null
    Write-Step "  Firewall rule added (Private/Domain profiles only, port $Port)."

    # Production audit 2026-08-20 (kb/status.md): Windows classifies a home Wi-Fi network Public
    # by default whenever the user declined (or was never asked about) network discovery -- a
    # very plausible state on a fresh Windows install, exactly what a new family laptop would be.
    # The rule above then silently does nothing and the phone simply can't reach the board, with
    # no diagnostic anywhere. Deliberately NOT auto-widening the rule to Public (the comment above
    # already made that call, for good reason) -- surfacing this loudly instead, so it's at least
    # visible in install_log.txt rather than a silent dead end.
    $currentProfile = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($currentProfile -and $currentProfile.NetworkCategory -eq "Public") {
        Write-WarnStep "  This network ('$($currentProfile.Name)') is classified Public by Windows."
        Write-WarnStep "  The firewall rule above will NOT allow phones to reach this NAS until that's fixed."
        Write-WarnStep "  If this is genuinely your own home network, run as Administrator:"
        Write-WarnStep "    Set-NetConnectionProfile -InterfaceIndex $($currentProfile.InterfaceIndex) -NetworkCategory Private"
    }
}

# ── 7b. Power settings (keep the NAS reachable when the lid closes) ────────────
# Found 2026-08-20 testing on real laptop hardware: LIDACTION defaulted to Sleep on both
# AC and DC, and was HIDDEN from `powercfg /query` by this OEM image (had to
# `-ATTRIB_HIDE:false` it to even see the current value) -- closing the lid would suspend
# the entire NAS, services included, exactly the failure mode a server install must not
# have. STANDBYIDLE (system sleep on idle timeout) needs the same fix for the same reason
# -- a NAS left alone for the idle timeout must not go offline either. Deliberately NOT
# touching MONITORIDLE (display-off timeout) here -- that's a real user's normal display
# behavior, not something a NAS installer should override; unlike lid-close/system-sleep,
# turning the screen off does not take the service down.
function Set-PowerSettings {
    Write-Step "[7b/9] Configuring power settings so the NAS stays up through lid-close and short power cuts..."
    # Both AC and DC (battery) now disable lid-close-sleep and idle-sleep -- revised 2026-08-20
    # after real-world feedback superseded the original AC-only design. That original design
    # (production audit 2026-08-20, kb/status.md) left DC at Windows' defaults specifically to
    # avoid draining an unattended, unplugged laptop indefinitely -- correct for "someone
    # forgot to plug it in," wrong for the actual use case that surfaced: a home power cut where
    # a generator recovers within minutes and the battery has 2+ hours of real runway. Under the
    # old AC-only design, Windows' default DC idle timer (~15-30 min) would take the NAS down
    # well before the battery was ever actually in danger -- the wrong tradeoff when the whole
    # point is riding out a short outage. The safety net for a genuine full-drain risk is now the
    # purpose-built critical-battery mechanism below, not an idle timer.
    #
    # NOT YET LIVE-VERIFIED as of 2026-08-20 -- the SUB_BATTERY BATLEVELCRIT/BATACTIONCRIT
    # aliases below are the standard, Microsoft-documented ones, but this exact machine hasn't
    # confirmed them via `powercfg /query SUB_BATTERY` the way LIDACTION's hidden-attribute
    # quirk was confirmed live before trusting it. Verify that first before relying on this.
    # Also unresolved: whether this machine's BIOS supports wake-on-AC after a hibernate --
    # if not, a real outage that reaches the 20% threshold leaves the machine off until someone
    # physically presses power. Confirm both before this ships to a real, unattended install.
    #
    # Applied to EVERY power scheme, not just SCHEME_CURRENT: switching plans (e.g. to Power
    # Saver) would otherwise silently re-enable sleep and take the NAS offline with no warning --
    # found during the same audit.
    & powercfg -attributes SUB_BUTTONS LIDACTION -ATTRIB_HIDE:false
    $schemeLines = & powercfg /list | Select-String -Pattern "Power Scheme GUID:\s+([0-9a-fA-F-]{36})"
    $schemeGuids = $schemeLines | ForEach-Object { $_.Matches[0].Groups[1].Value }
    if (-not $schemeGuids) {
        # powercfg /list output format changed or is unparseable -- fall back to just the active
        # scheme rather than silently configuring nothing.
        $schemeGuids = @("SCHEME_CURRENT")
    }
    # Critical battery level/action -- the actual safety net now that idle-based DC sleep is
    # disabled above. Deliberately hibernate, not sleep, at the threshold: hibernate writes state
    # to disk and powers off completely (~0% further drain, and a clean shutdown that protects
    # the TLS/database files on disk), where sleep keeps drawing some power and risks an
    # uncontrolled hard power-off at 0% if an outage runs longer than expected. 20% leaves a large
    # real margin given a 2-hour DC runway and outages that typically recover in minutes. Whether
    # the machine auto-resumes once AC power returns depends on this laptop's own BIOS wake-on-AC
    # support, which this script has no way to detect or guarantee -- flagged as a real, unverified
    # limitation, not assumed to work.
    & powercfg -attributes SUB_BATTERY BATLEVELCRIT -ATTRIB_HIDE:false 2>$null | Out-Null
    & powercfg -attributes SUB_BATTERY BATACTIONCRIT -ATTRIB_HIDE:false 2>$null | Out-Null
    foreach ($guid in $schemeGuids) {
        & powercfg /setacvalueindex $guid SUB_BUTTONS LIDACTION 0 2>$null | Out-Null  # 0 = Do nothing
        & powercfg /setdcvalueindex $guid SUB_BUTTONS LIDACTION 0 2>$null | Out-Null
        & powercfg /setacvalueindex $guid SUB_SLEEP STANDBYIDLE 0 2>$null | Out-Null  # 0 = never
        & powercfg /setdcvalueindex $guid SUB_SLEEP STANDBYIDLE 0 2>$null | Out-Null
        & powercfg /setdcvalueindex $guid SUB_BATTERY BATLEVELCRIT 20 2>$null | Out-Null   # 20%
        & powercfg /setdcvalueindex $guid SUB_BATTERY BATACTIONCRIT 2 2>$null | Out-Null   # 2 = Hibernate
    }
    & powercfg /setactive SCHEME_CURRENT
    Write-Step "  Lid-close and idle-sleep disabled on AC and battery, across all $($schemeGuids.Count) power plan(s). Battery safety net: hibernates at 20% remaining instead of an idle timer."
}

# ── 7c. Remote access (optional, Tailscale) ─────────────────────────────────────
# Mirrors the Android app's own Remote Access step (InstallerCommands.kt's installTailscale/
# tailscaleUp): install the official client, start enrolment, surface the login URL for one
# approval tap. Unlike Android's board-provisioning flow, this runs locally on the same machine
# the user is sitting at, over msiexec/tailscale.exe directly -- no SSH round trip needed.
#
# NOT YET LIVE-VERIFIED as of 2026-08-20 -- the download URL/hash were verified for real (see
# $TailscaleMsiSha256's own comment above), and `msiexec /quiet` plus `tailscale up`'s stdout
# login-URL format are used per their long-documented, stable behavior, but this exact sequence
# has not been run on a real Windows machine yet. Verify end-to-end (including that the log-file
# scrape below actually finds the URL) before relying on it for a real install.
function Install-RemoteAccess {
    if (-not $EnableRemoteAccess) {
        Write-Step "[7c/9] Remote access (Tailscale) -- skipped (opt-in only; pass -EnableRemoteAccess to set it up)."
        return
    }
    Write-Step "[7c/9] Setting up remote access (Tailscale)..."
    $tsExe = "$env:ProgramFiles\Tailscale\tailscale.exe"

    if (-not (Test-Path $tsExe)) {
        $tmpMsi = Join-Path $env:TEMP "ahc_tailscale_setup.msi"
        Write-Step "  Downloading Tailscale $TailscaleVersion..."
        Invoke-WebRequest -Uri $TailscaleMsiUrl -OutFile $tmpMsi -UseBasicParsing
        Assert-FileHash $tmpMsi $TailscaleMsiSha256 "Tailscale installer"
        Write-Step "  Integrity verified (SHA-256 match). Installing..."
        # /quiet /norestart -- standard silent-MSI convention, same idea as this script's own
        # NSSM/Python provisioning: no UI, no reboot prompt for a service host nobody is watching.
        # Array form, not a single quoted string -- Start-Process's own argument marshaling
        # handles a space in $tmpMsi correctly per-element; hand-wrapping in embedded quotes here
        # would double-quote it instead.
        $proc = Start-Process msiexec.exe -ArgumentList "/i", $tmpMsi, "/quiet", "/norestart" -Wait -PassThru
        Remove-Item $tmpMsi -Force
        if ($proc.ExitCode -ne 0) {
            Write-WarnStep "  Tailscale install failed (msiexec exit $($proc.ExitCode)). Remote access skipped -- the NAS itself is unaffected, this is optional."
            return
        }
        if (-not (Test-Path $tsExe)) {
            Write-WarnStep "  Tailscale installer reported success but tailscale.exe is missing at the expected path. Remote access skipped."
            return
        }
        Write-Step "  Tailscale installed."
    } else {
        Write-Step "  Tailscale already installed."
    }

    # Detached and logged to a file, not captured inline -- `tailscale up` blocks until a human
    # approves in a browser, which must not block the rest of this installer. Same reasoning as
    # the Android wizard's board-side equivalent (InstallerCommands.kt's tailscaleUp doc).
    # --ssh=false explicit for the same reason documented there: a default personal-tailnet ACL
    # would otherwise let any tailnet node request a shell on this NAS.
    $tsUpLog = Join-Path $env:TEMP "ahc_tailscale_up.log"
    Remove-Item $tsUpLog -Force -ErrorAction SilentlyContinue
    $hostname = "aihomecloud-" + ($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9-]', '')
    Start-Process -FilePath $tsExe -ArgumentList "up --hostname=$hostname --ssh=false" `
        -RedirectStandardOutput $tsUpLog -RedirectStandardError "$tsUpLog.err" -WindowStyle Hidden

    Write-Step "  Waiting for a login URL (up to 30s)..."
    $loginUrl = $null
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Path $tsUpLog) {
            $match = Select-String -Path $tsUpLog -Pattern "https://login\.tailscale\.com/\S+" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($match) { $loginUrl = $match.Matches[0].Value; break }
        }
    }
    if ($loginUrl) {
        Write-Step "  Opening $loginUrl -- approve this PC to finish, whenever's convenient."
        Start-Process $loginUrl
    } else {
        Write-WarnStep "  No login URL seen yet. Remote access setup is still running in the background --"
        Write-WarnStep "  check https://login.tailscale.com/admin/machines, or run 'tailscale up' again later."
    }
}

# ── 8. Start ───────────────────────────────────────────────────────────────────
function Start-AiHomeCloudService {
    Write-Step "[8/9] Starting service..."
    & $NssmExe start $ServiceName
    Start-Sleep -Seconds 3

    # Generous budget: TLS cert resolution alone can take up to ~30s when issuance isn't
    # available yet (the common case on Windows today, since no cert-issuance daemon exists
    # here -- see tls.py's _REISSUE_TIMEOUT_S and main.py's __main__ block). 25 * 2s = 50s,
    # plus the 3s above, covers that with real margin. 5 attempts (the old budget, ~13-28s)
    # was never enough even before accounting for the two bugs below.
    $ok = $false
    for ($i = 1; $i -le 25; $i++) {
        # curl.exe, not Invoke-WebRequest -SkipCertificateCheck -- that parameter is
        # PowerShell 6+ only and does not exist on Windows PowerShell 5.1, so this health
        # check could never have succeeded here regardless of server health. curl.exe ships
        # in-box on Windows 10 1803+ / Server 2019+, same baseline this script already
        # assumes for tar.exe.
        # Try HTTPS first (the steady-state case once Windows has real cert issuance), fall
        # back to HTTP (today's actual case -- TLS silently falls back to plain HTTP here).
        $httpsCode = & curl.exe -sk -o NUL -w "%{http_code}" --max-time 3 "https://localhost:$Port/api/health" 2>$null
        if ($httpsCode -eq "200") { $ok = $true; break }
        $httpCode = & curl.exe -s -o NUL -w "%{http_code}" --max-time 3 "http://localhost:$Port/api/health" 2>$null
        if ($httpCode -eq "200") { $ok = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ok) {
        Write-WarnStep "  Health check did not respond after $i attempts — check:"
        Write-WarnStep "    Get-Content '$DataDir\logs\service.err.log' -Tail 50"
        throw "Service did not become healthy."
    }
    Write-Step "  Health check passed."
}

# ── 9. Summary ─────────────────────────────────────────────────────────────────
function Write-Summary {
    Write-Step "[9/9] === Installation Complete! ==="
    Write-Host ""
    Write-Host "  Backend URL  : https://localhost:$Port" -ForegroundColor Cyan
    Write-Host "  Data dir     : $DataDir" -ForegroundColor Cyan
    Write-Host "  NAS root     : $NasRoot" -ForegroundColor Cyan
    Write-Host "  Service logs : $DataDir\logs\" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor Yellow
    Write-Host "  1. Open the AiHomeCloud app on your phone"
    Write-Host "  2. Tap Auto-Discover to find this PC on your network"
    Write-Host "  3. Point NAS root ($NasRoot) at a drive with real space, if the default isn't right"
    Write-Host "  4. A tray icon (Start/Stop server, Open Dashboard) appears next time you sign in"
    Write-Host ""
    Write-Host "  To uninstall: nssm remove $ServiceName confirm" -ForegroundColor Yellow
    Write-Host "  (this stops and removes the SERVICE only — $DataDir and $NasRoot are never touched)"
    Write-Host ""
}

# ── Main ─────────────────────────────────────────────────────────────────────
try {
    Write-Step "=== AiHomeCloud Windows Installer ==="
    Test-NotDomainJoined
    Test-SafeNasRoot
    New-Directories
    New-ServiceAccount
    Stop-ExistingServices
    Install-Python
    Copy-BackendCode
    New-Venv
    Install-SemanticSearch
    New-Secrets
    Install-Service
    Grant-ServiceControlToUsers
    Install-TrayAutostart
    Install-CertIssuerService
    New-FirewallRule
    Set-PowerSettings
    Install-RemoteAccess
    Start-AiHomeCloudService
    Write-Summary
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
