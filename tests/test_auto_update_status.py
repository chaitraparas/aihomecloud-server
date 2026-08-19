"""
The board must be able to say whether it is actually patching itself.

This exists because all three boards in the fleet were "configured and enabled" while applying
nothing, in three different ways, with nothing logged: a Package-Blacklist glob parsed as a regex
threw and aborted every run; an Origins-Pattern naming the wrong distribution matched no repository
at all. The tests therefore focus on the distinction that matters — ran-and-had-nothing-to-do
versus crashed-before-looking — because a status that conflates those is worse than none.
"""

import pytest

from app.routes import system_routes as sr


@pytest.fixture
def log(tmp_path, monkeypatch):
    def _write(text):
        p = tmp_path / "uu.log"
        p.write_text(text)
        monkeypatch.setattr(sr, "_UU_LOG", p)
        monkeypatch.setattr(sr, "_UU_LOG_MIRROR", tmp_path / "absent-mirror")
        monkeypatch.setattr(sr, "_UU_ENABLE", tmp_path / "absent")
        monkeypatch.setattr(sr, "_AHC_UU_CONF", tmp_path / "absent2")
        return p
    return _write


class TestDistinguishingSuccessFromSilence:
    def test_a_clean_run_with_nothing_to_do_counts_as_success(self, log):
        log("2026-08-08 03:00:01,000 INFO No packages found that can be upgraded\n")

        s = sr._read_auto_update_status()

        assert s["lastSuccessAt"] == "2026-08-08 03:00:01"
        assert s["lastError"] is None

    def test_a_crash_is_not_reported_as_success(self, log):
        """The real failure: it aborted before looking, and everything else looked fine."""
        log(
            "2026-08-08 03:00:01,000 INFO Starting unattended upgrades script\n"
            "2026-08-08 03:00:02,000 ERROR re.error: nothing to repeat at position 0\n"
        )

        s = sr._read_auto_update_status()

        assert s["lastSuccessAt"] is None
        assert "nothing to repeat" in s["lastError"]

    def test_a_later_success_clears_an_earlier_error(self, log):
        log(
            "2026-08-07 03:00:02,000 ERROR re.error: nothing to repeat at position 0\n"
            "2026-08-08 03:00:09,000 INFO All upgrades installed\n"
        )

        s = sr._read_auto_update_status()

        assert s["lastSuccessAt"] == "2026-08-08 03:00:09"
        assert s["lastError"] is None

    def test_upgraded_package_count_is_read(self, log):
        log(
            "2026-08-08 03:00:03,000 INFO Packages that will be upgraded: libssl3 curl tzdata\n"
            "2026-08-08 03:00:09,000 INFO All upgrades installed\n"
        )

        assert sr._read_auto_update_status()["packagesUpgraded"] == 3


class TestDegradingSafely:
    def test_a_missing_log_reports_unknown_rather_than_raising(self, tmp_path, monkeypatch):
        """A board that has never run this must not 500 the settings screen."""
        monkeypatch.setattr(sr, "_UU_LOG", tmp_path / "nope.log")
        monkeypatch.setattr(sr, "_UU_LOG_MIRROR", tmp_path / "nope.mirror")
        monkeypatch.setattr(sr, "_UU_ENABLE", tmp_path / "nope")
        monkeypatch.setattr(sr, "_AHC_UU_CONF", tmp_path / "nope2")

        s = sr._read_auto_update_status()

        assert s["enabled"] is False and s["lastRunAt"] is None

    def test_kernel_policy_is_read_from_the_generated_header(self, tmp_path, monkeypatch):
        conf = tmp_path / "51ahc"
        # The exact string the generator writes. An earlier version of the parser looked for
        # "-> vendor" and reported "unknown" against a correctly-configured board.
        conf.write_text("// Running kernel: 5.15.0-aw2501 (package: x) -> policy: vendor\n")
        monkeypatch.setattr(sr, "_AHC_UU_CONF", conf)
        monkeypatch.setattr(sr, "_UU_LOG", tmp_path / "nope.log")
        monkeypatch.setattr(sr, "_UU_LOG_MIRROR", tmp_path / "nope.mirror")
        monkeypatch.setattr(sr, "_UU_ENABLE", tmp_path / "nope")

        assert sr._read_auto_update_status()["kernelPolicy"] == "vendor"

    def test_the_mirror_is_preferred_over_the_root_owned_log(self, tmp_path, monkeypatch):
        """
        The real log lives in a root:adm 0750 directory the service cannot traverse, so a drop-in
        mirrors it into the service's own data dir. If both are readable the mirror wins, because
        it is the one that stays readable in production.
        """
        mirror = tmp_path / "mirror.log"
        mirror.write_text("2026-08-08 03:00:09,000 INFO All upgrades installed\n")
        real = tmp_path / "real.log"
        real.write_text("2026-01-01 03:00:02,000 ERROR stale\n")
        monkeypatch.setattr(sr, "_UU_LOG_MIRROR", mirror)
        monkeypatch.setattr(sr, "_UU_LOG", real)
        monkeypatch.setattr(sr, "_UU_ENABLE", tmp_path / "nope")
        monkeypatch.setattr(sr, "_AHC_UU_CONF", tmp_path / "nope2")

        s = sr._read_auto_update_status()

        assert s["lastSuccessAt"] == "2026-08-08 03:00:09"
        assert s["lastError"] is None


class TestMaintenanceWindow:
    """
    The household picks the hours; the board obeys them.

    The validation tests matter more than they look. The endpoint's value reaches a helper that
    writes a systemd unit as root, so anything that is not a plain HH:MM is a line-injection
    attempt on a root-run file. It is rejected at the model, and again in the helper — the helper
    does not trust that this layer ran.
    """

    async def test_a_board_with_no_choice_stored_reports_the_default(self, client, monkeypatch):
        monkeypatch.setattr(sr, "_WINDOW_REQUEST", sr.Path("/nonexistent/window.json"))

        w = sr._read_window()

        assert w["start"] == "03:00" and w["durationHours"] == 2

    async def test_a_stored_choice_is_returned(self, tmp_path, monkeypatch):
        f = tmp_path / "w.json"
        f.write_text('{"start": "01:30", "durationHours": 4, "enabled": true}')
        monkeypatch.setattr(sr, "_WINDOW_REQUEST", f)

        w = sr._read_window()

        assert w["start"] == "01:30" and w["durationHours"] == 4

    async def test_corrupt_stored_json_falls_back_instead_of_raising(self, tmp_path, monkeypatch):
        """A settings screen must render even when the file underneath it is damaged."""
        f = tmp_path / "w.json"
        f.write_text("{not json")
        monkeypatch.setattr(sr, "_WINDOW_REQUEST", f)

        assert sr._read_window()["start"] == "03:00"

    @pytest.mark.parametrize("bad", [
        "24:00", "3:00", "03:60", "", "03:00\nOnCalendar=*-*-* 00:00", "03:00; rm -rf /",
    ])
    def test_a_time_that_is_not_plain_hhmm_is_refused(self, bad):
        from pydantic import ValidationError
        from app.models import MaintenanceWindow

        with pytest.raises(ValidationError):
            MaintenanceWindow(start=bad)

    @pytest.mark.parametrize("hours", [0, 13, -1])
    def test_an_out_of_range_duration_is_refused(self, hours):
        from pydantic import ValidationError
        from app.models import MaintenanceWindow

        with pytest.raises(ValidationError):
            MaintenanceWindow(durationHours=hours)

    def test_a_sensible_window_is_accepted(self):
        from app.models import MaintenanceWindow

        w = MaintenanceWindow(start="23:45", durationHours=6)

        assert w.start == "23:45" and w.durationHours == 6
