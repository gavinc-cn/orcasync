import os

from orcasync.conflict import (
    CONCURRENT_MTIME_WINDOW_S,
    conflict_filename,
    detect_conflict,
    pick_loser,
    preserve_local_as_conflict,
)


def _entry(*, mtime=100.0, blocks=None, is_dir=False, size=10):
    if blocks is None:
        blocks = [{"index": 0, "size": size, "hash": "abc"}]
    return {"mtime": mtime, "blocks": blocks, "is_dir": is_dir, "size": size}


class TestDetectConflict:
    def test_missing_side_is_not_conflict(self):
        assert detect_conflict(None, _entry()) is False
        assert detect_conflict(_entry(), None) is False

    def test_identical_hashes_is_not_conflict(self):
        a = _entry(mtime=100.0)
        b = _entry(mtime=100.0)
        assert detect_conflict(a, b) is False

    def test_close_mtimes_with_different_hashes_is_conflict(self):
        a = _entry(mtime=100.0, blocks=[{"index": 0, "size": 10, "hash": "AAA"}])
        b = _entry(mtime=101.0, blocks=[{"index": 0, "size": 10, "hash": "BBB"}])
        assert detect_conflict(a, b) is True

    def test_far_apart_mtimes_is_not_conflict(self):
        a = _entry(mtime=100.0, blocks=[{"index": 0, "size": 10, "hash": "AAA"}])
        b = _entry(mtime=100.0 + CONCURRENT_MTIME_WINDOW_S + 10,
                   blocks=[{"index": 0, "size": 10, "hash": "BBB"}])
        assert detect_conflict(a, b) is False

    def test_directories_are_not_conflicts(self):
        a = _entry(is_dir=True)
        b = _entry(is_dir=True)
        assert detect_conflict(a, b) is False


class TestConflictFilename:
    def test_includes_timestamp_and_ext(self):
        # 2026-05-15 10:30:45 in local time → just check shape
        name = conflict_filename("docs/notes.md", now=1747297845.0, host="alpha")
        assert name.startswith("docs/notes.sync-conflict-")
        assert name.endswith("-alpha.md")

    def test_handles_no_extension(self):
        name = conflict_filename("README", now=1747297845.0, host="alpha")
        assert name.startswith("README.sync-conflict-")
        assert name.endswith("-alpha")

    def test_handles_root_level_file(self):
        name = conflict_filename("a.txt", now=0, host="h")
        # No leading slash; same dir as original
        assert "/" not in name
        assert name.startswith("a.sync-conflict-")


class TestPickLoser:
    def test_older_mtime_loses(self):
        a = _entry(mtime=100.0)
        b = _entry(mtime=200.0)
        assert pick_loser(a, b) == "local"  # local is older
        assert pick_loser(b, a) == "remote"

    def test_mtime_tie_breaks_by_host(self):
        a = _entry(mtime=100.0)
        b = _entry(mtime=100.0)
        # bigger host string loses
        assert pick_loser(a, b, local_host="zzz", remote_host="aaa") == "local"
        assert pick_loser(a, b, local_host="aaa", remote_host="zzz") == "remote"


class TestPreserveLocalAsConflict:
    def test_renames_existing_file(self, tmp_path):
        (tmp_path / "f.txt").write_text("local content")
        result = preserve_local_as_conflict(str(tmp_path), "f.txt", now=1747297845.0)
        assert result is not None
        assert result.startswith("f.sync-conflict-")
        assert result.endswith(".txt")
        assert not (tmp_path / "f.txt").exists()
        assert (tmp_path / result).read_text() == "local content"

    def test_no_op_when_file_missing(self, tmp_path):
        assert preserve_local_as_conflict(str(tmp_path), "ghost.txt") is None

    def test_preserves_subdirectory(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("x")
        result = preserve_local_as_conflict(
            str(tmp_path), "sub/a.txt", now=1747297845.0
        )
        assert result is not None
        assert result.startswith("sub/a.sync-conflict-")
        assert (tmp_path / result.replace("/", os.sep)).exists()
