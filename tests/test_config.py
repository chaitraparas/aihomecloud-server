from pathlib import Path

from app.config import generate_jwt_secret


def test_generate_jwt_secret_persists(tmp_path: Path):
    secret_file = tmp_path / "jwt_secret"
    # First call should create the file and return a hex string
    s1 = generate_jwt_secret(secret_file)
    assert secret_file.exists(), "secret file must be created"
    assert isinstance(s1, str) and len(s1) == 64, "secret should be 64 hex chars"

    # Second call should return the same value
    s2 = generate_jwt_secret(secret_file)
    assert s1 == s2, "generate_jwt_secret should return same secret on repeated calls"
    # The file content should match
    assert secret_file.read_text().strip() == s1
