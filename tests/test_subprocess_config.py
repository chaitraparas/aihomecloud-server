"""
Additional tests for subprocess_runner.py and config helpers.
"""

import sys
import pytest
from unittest.mock import patch, AsyncMock


class TestSubprocessRunner:
    @pytest.mark.asyncio
    async def test_empty_cmd_raises(self):
        from app.subprocess_runner import run_command
        with pytest.raises(ValueError):
            await run_command([])

    @pytest.mark.asyncio
    async def test_non_list_raises(self):
        from app.subprocess_runner import run_command
        with pytest.raises(ValueError):
            await run_command("ls -la")

    @pytest.mark.asyncio
    async def test_shell_metachars_pass_through_as_literal_arg(self):
        """M6: run_command no longer rejects tokens containing shell metacharacters — since
        create_subprocess_exec never invokes a shell, "hello; rm -rf /" is one literal argv
        entry, not shell syntax. Prove it's passed through unexecuted and unmodified."""
        from app.subprocess_runner import run_command
        rc, out, err = await run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "hello; rm -rf /"]
        )
        assert rc == 0
        assert out == "hello; rm -rf /"

    @pytest.mark.asyncio
    async def test_pipe_metachar_passes_through_as_literal_arg(self):
        from app.subprocess_runner import run_command
        rc, out, err = await run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "file | grep x"]
        )
        assert rc == 0
        assert out == "file | grep x"

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        from app.subprocess_runner import run_command
        rc, out, err = await run_command(["nonexistent_command_xyz123"])
        assert rc == -1
        assert "not_found" in err

    @pytest.mark.asyncio
    async def test_successful_command(self):
        from app.subprocess_runner import run_command
        rc, out, err = await run_command([sys.executable, "-c", "print('hello')"])
        assert rc == 0
        assert "hello" in out

    @pytest.mark.asyncio
    async def test_nonzero_exit(self):
        from app.subprocess_runner import run_command
        rc, out, err = await run_command([sys.executable, "-c", "raise SystemExit(1)"])
        assert rc == 1


class TestConfigHelpers:
    def test_get_local_ip(self):
        from app.config import get_local_ip
        ip = get_local_ip()
        assert isinstance(ip, str)
        assert len(ip) > 0

    def test_generate_device_serial(self):
        from app.config import generate_device_serial
        serial = generate_device_serial()
        assert isinstance(serial, str)
        assert len(serial) > 0

    def test_settings_has_required_fields(self):
        from app.config import settings
        assert hasattr(settings, "nas_root")
        assert hasattr(settings, "data_dir")
        assert hasattr(settings, "jwt_secret")
        assert hasattr(settings, "backend_version")


class TestHygiene:
    def test_scan_test_artifacts_no_files(self, tmp_path):
        from app.hygiene import _scan_test_artifacts
        with patch("app.hygiene.settings") as mock_settings:
            mock_settings.personal_path = tmp_path / "personal"
            mock_settings.family_path = tmp_path / "shared"
            (tmp_path / "personal").mkdir()
            (tmp_path / "shared").mkdir()
            result = _scan_test_artifacts()
            assert result == []

    def test_scan_test_artifacts_finds_files(self, tmp_path):
        from app.hygiene import _scan_test_artifacts
        with patch("app.hygiene.settings") as mock_settings:
            personal = tmp_path / "personal"
            personal.mkdir()
            (personal / "hwtest_abc.txt").write_text("test")
            mock_settings.personal_path = personal
            mock_settings.family_path = tmp_path / "shared"
            (tmp_path / "shared").mkdir()
            result = _scan_test_artifacts()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_cleanup_startup_artifacts(self, tmp_path):
        from app.hygiene import cleanup_startup_artifacts
        with patch("app.hygiene.settings") as mock_settings, \
             patch("app.hygiene.remove_documents_by_filename_patterns", new_callable=AsyncMock, return_value=0), \
             patch("app.hygiene.remove_missing_documents", new_callable=AsyncMock, return_value=0):
            personal = tmp_path / "personal"
            personal.mkdir()
            (personal / "hwtest_001.txt").write_text("test")
            mock_settings.personal_path = personal
            mock_settings.family_path = tmp_path / "shared"
            (tmp_path / "shared").mkdir()
            result = await cleanup_startup_artifacts()
            assert result["deleted_files"] == 1
