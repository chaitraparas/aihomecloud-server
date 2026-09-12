"""
ahc-apply-backend-update.sh's old guard only rejected tar entry NAMES containing ".." or a
leading "/" (via `tar -tf`) -- it never inspected a symlink/hardlink entry's TARGET. Verified
empirically against GNU tar 1.35 (the version on the boards): a symlink entry with a clean
name and an escaping linkname extracts cleanly and is never caught by that check.

`test_old_name_only_guard_missed_the_escaping_symlink` reproduces exactly that gap using the
old guard's own predicate, proving it lets the malicious entry through. Everything else proves
scripts.extract_update_bundle.safe_extract (tarfile.data_filter) rejects it -- and every other
member shape the fix is required to defend against -- while still extracting a legitimate
bundle untouched.
"""

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "extract_update_bundle.py"
_spec = importlib.util.spec_from_file_location("extract_update_bundle", _MODULE_PATH)
extract_update_bundle = importlib.util.module_from_spec(_spec)
sys.modules["extract_update_bundle"] = extract_update_bundle
_spec.loader.exec_module(extract_update_bundle)

safe_extract = extract_update_bundle.safe_extract


def _old_guard_would_reject(names: list[str]) -> bool:
    """The exact predicate the old bash guard used: `[[ "$entry" == /* || "$entry" == *".."* ]]`."""
    return any(name.startswith("/") or ".." in name for name in names)


def _add_symlink(tf: tarfile.TarFile, name: str, linkname: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.SYMTYPE
    info.linkname = linkname
    tf.addfile(info)


def _add_hardlink(tf: tarfile.TarFile, name: str, linkname: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.LNKTYPE
    info.linkname = linkname
    tf.addfile(info)


def _add_file(tf: tarfile.TarFile, name: str, content: bytes = b"x") -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    tf.addfile(info, io.BytesIO(content))


class TestOldGuardMissedSymlinkTargets:
    def test_old_name_only_guard_missed_the_escaping_symlink(self, tmp_path):
        """Reproduces the vulnerable behaviour: a clean entry NAME with an escaping symlink
        TARGET sails past the old bash predicate untouched."""
        tar_path = tmp_path / "bundle.tar"
        with tarfile.open(tar_path, "w") as tf:
            _add_symlink(tf, "app/config.py", linkname="../../../etc/whatever")

        with tarfile.open(tar_path) as tf:
            names = tf.getnames()

        assert names == ["app/config.py"]
        assert _old_guard_would_reject(names) is False, (
            "the old guard's own predicate does not flag this entry -- "
            "it only ever looked at names, never link targets"
        )


class TestSafeExtractRejectsEscapes:
    def test_rejects_symlink_entry_with_escaping_target(self, tmp_path):
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_symlink(tf, "app/config.py", linkname="../../../etc/whatever")

        with pytest.raises(tarfile.TarError):
            safe_extract(str(tar_path), str(dest))
        assert not (dest / "app" / "config.py").exists()

    def test_rejects_hardlink_entry_with_escaping_target(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("sensitive")
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_hardlink(tf, "app/linked", linkname="../outside.txt")

        with pytest.raises(tarfile.TarError):
            safe_extract(str(tar_path), str(dest))

    def test_absolute_entry_name_is_clamped_inside_dest_not_written_to_the_real_path(self, tmp_path):
        """Matches tar's own defence for absolute names: the leading "/" is stripped and the
        member lands under `dest`, never at the literal absolute path."""
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_file(tf, "/etc/passwd", b"not the real /etc/passwd")

        safe_extract(str(tar_path), str(dest))

        assert (dest / "etc" / "passwd").read_bytes() == b"not the real /etc/passwd"

    def test_rejects_dotdot_traversal_in_entry_name(self, tmp_path):
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_file(tf, "../../etc/passwd")

        with pytest.raises(tarfile.TarError):
            safe_extract(str(tar_path), str(dest))

    def test_rejects_nested_traversal_plus_symlink_combination(self, tmp_path):
        """A symlink entry whose own NAME embeds traversal AND whose TARGET also escapes --
        neither should be enough alone, and the combination must not slip through either."""
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_symlink(tf, "app/../evil", linkname="../../etc/whatever")

        with pytest.raises(tarfile.TarError):
            safe_extract(str(tar_path), str(dest))


class TestSafeExtractStillWorksForALegitimateBundle:
    def test_extracts_a_normal_bundle_unchanged(self, tmp_path):
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_file(tf, "app/main.py", b"print('hi')\n")
            _add_file(tf, "requirements.txt", b"fastapi\n")

        safe_extract(str(tar_path), str(dest))

        assert (dest / "app" / "main.py").read_bytes() == b"print('hi')\n"
        assert (dest / "requirements.txt").read_bytes() == b"fastapi\n"


class TestMainCli:
    def test_main_returns_nonzero_and_cleans_up_on_rejection(self, tmp_path):
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_symlink(tf, "app/config.py", linkname="../../../etc/whatever")

        rc = extract_update_bundle.main(["extract_update_bundle.py", str(tar_path), str(dest)])

        assert rc == 1
        assert not dest.exists()

    def test_main_returns_zero_on_a_clean_bundle(self, tmp_path):
        tar_path = tmp_path / "bundle.tar"
        dest = tmp_path / "dest"
        with tarfile.open(tar_path, "w") as tf:
            _add_file(tf, "app/main.py", b"print('hi')\n")

        rc = extract_update_bundle.main(["extract_update_bundle.py", str(tar_path), str(dest)])

        assert rc == 0
        assert (dest / "app" / "main.py").exists()
