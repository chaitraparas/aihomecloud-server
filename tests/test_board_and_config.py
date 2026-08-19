"""
Board detection and config generation tests.
"""

import pytest
from pathlib import Path


def test_generate_jwt_secret_creates_file(tmp_path):
    """JWT secret is generated and persisted."""
    from app.config import generate_jwt_secret
    secret_file = tmp_path / "jwt_secret"

    s1 = generate_jwt_secret(secret_file)
    assert secret_file.exists()
    assert len(s1) == 64  # 32 bytes hex = 64 chars

    # Idempotent
    s2 = generate_jwt_secret(secret_file)
    assert s1 == s2


def test_generate_pairing_key_creates_file(tmp_path):
    """Pairing key is generated and persisted."""
    from app.config import generate_pairing_key
    key_file = tmp_path / "pairing_key"

    k1 = generate_pairing_key(key_file)
    assert key_file.exists()
    assert len(k1) > 10  # url-safe base64

    k2 = generate_pairing_key(key_file)
    assert k1 == k2


def test_generate_device_serial():
    """Device serial is generated from MAC address."""
    from app.config import generate_device_serial

    serial = generate_device_serial()
    assert serial.startswith("AHC-")
    assert len(serial) == 10  # AHC- + 6 hex chars


def test_generate_hotspot_password():
    """Hotspot password is random and 12 chars."""
    from app.config import generate_hotspot_password

    pw = generate_hotspot_password()
    assert len(pw) == 12
    # Should be different each call
    pw2 = generate_hotspot_password()
    assert pw != pw2


def test_get_local_ip():
    """get_local_ip returns a valid IP string."""
    from app.config import get_local_ip

    ip = get_local_ip()
    assert ip  # not empty
    # Should be a valid IPv4 format
    parts = ip.split(".")
    assert len(parts) == 4


def test_settings_properties():
    """Settings computed properties return correct paths."""
    from app.config import settings

    assert settings.personal_path == settings.nas_root / settings.personal_base
    assert settings.family_path == settings.nas_root / settings.family_dir
    assert settings.entertainment_path == settings.nas_root / settings.entertainment_dir
    assert settings.users_file == settings.data_dir / "users.json"
    assert settings.services_file == settings.data_dir / "services.json"


def test_cors_origins_parsing():
    """CORS origins can be parsed from comma-separated string."""
    from app.config import Settings

    s = Settings(cors_origins="http://a.com,http://b.com")
    assert s.cors_origins == ["http://a.com", "http://b.com"]


def test_cors_wildcard_origin_rejected_string_input():
    """
    L2 regression: AHC_CORS_ORIGINS=* must be rejected outright. main.py always sends
    allow_credentials=True on the CORS middleware; a wildcard origin combined with credentials
    is refused by browsers per the CORS spec (silently non-functional) and is a
    credential-exposure risk for any client that doesn't enforce that spec restriction.
    """
    import pytest as _pytest
    from pydantic import ValidationError

    from app.config import Settings

    with _pytest.raises(ValidationError, match="cannot include '\\*'"):
        Settings(cors_origins="*")


def test_cors_wildcard_origin_rejected_when_mixed_with_real_origins():
    """A wildcard anywhere in the list must reject the whole config, not just drop the '*'
    entry silently — a partial fix here could still leave the operator's config believing '*'
    is respected when it's actually just missing."""
    import pytest as _pytest
    from pydantic import ValidationError

    from app.config import Settings

    with _pytest.raises(ValidationError, match="cannot include '\\*'"):
        Settings(cors_origins="http://a.com,*,http://b.com")


def test_cors_wildcard_origin_rejected_list_input():
    import pytest as _pytest
    from pydantic import ValidationError

    from app.config import Settings

    with _pytest.raises(ValidationError, match="cannot include '\\*'"):
        Settings(cors_origins=["http://a.com", "*"])


def test_board_detection():
    """Board detection returns a valid BoardConfig."""
    from app.board import detect_board

    board = detect_board()
    assert board.model_name  # not empty
    assert board.thermal_zone_path  # auto-detected
    assert board.lan_interface  # auto-detected


def test_find_thermal_zone():
    """Thermal zone detection returns a path."""
    from app.board import find_thermal_zone

    path = find_thermal_zone()
    assert path  # not empty
    assert "thermal_zone" in path


def test_find_lan_interface():
    """LAN interface detection returns an interface name."""
    from app.board import find_lan_interface

    iface = find_lan_interface()
    assert isinstance(iface, str)
    assert len(iface) > 0  # must return *some* interface name


# ── Security audit verifications (P6-03) ─────────────────────────────────────


def test_jwt_secret_auto_generated_is_at_least_32_bytes(tmp_path):
    """Auto-generated JWT secret must be ≥32 bytes (64 hex chars)."""
    from app.config import generate_jwt_secret

    secret = generate_jwt_secret(tmp_path / "jwt_secret_audit")
    assert len(secret) >= 64, f"JWT secret too short: {len(secret)} chars"


def test_jwt_secret_is_not_default_when_auto_generated(tmp_path):
    """Auto-generated JWT secret must not be the placeholder default."""
    from app.config import generate_jwt_secret

    secret = generate_jwt_secret(tmp_path / "jwt_secret_audit2")
    assert secret != "change-me-in-production"


def test_cors_default_does_not_include_wildcard():
    """Default CORS origins must not be a wildcard."""
    from app.config import Settings

    s = Settings()
    assert "*" not in s.cors_origins, "CORS wildcard '*' must not be in default origins"


@pytest.mark.asyncio
async def test_cors_evil_origin_rejected(client):
    """An untrusted origin must not receive Access-Control-Allow-Origin."""
    resp = await client.get(
        "/api/v1/system/info",
        headers={"Origin": "https://evil.example.com"},
    )
    acao = resp.headers.get("access-control-allow-origin", "")
    assert acao not in ("*", "https://evil.example.com"), (
        f"CORS wildcard or evil origin reflected: {acao!r}"
    )
