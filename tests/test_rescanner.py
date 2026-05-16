import asyncio
import os

import pytest

from orcasync.rescanner import PeriodicRescanner, _diff_for_changes
from orcasync.sync_engine import scan_directory


class TestDiffForChanges:
    def _make_file(self, path="a.txt", *, mtime=100.0, size=10, blocks=None):
        if blocks is None:
            blocks = [{"index": 0, "size": size, "hash": "abc"}]
        return {"path": path, "mtime": mtime, "size": size, "is_dir": False, "blocks": blocks}

    def test_no_change_yields_no_events(self):
        m = {"a.txt": self._make_file()}
        added, modified, deleted = _diff_for_changes(m, m)
        assert added == [] and modified == [] and deleted == []

    def test_new_file_appears_as_added(self):
        old = {}
        new = {"a.txt": self._make_file()}
        added, modified, deleted = _diff_for_changes(old, new)
        assert added == [("a.txt", False)]
        assert modified == [] and deleted == []

    def test_missing_file_appears_as_deleted(self):
        old = {"a.txt": self._make_file()}
        new = {}
        added, modified, deleted = _diff_for_changes(old, new)
        assert deleted == [("a.txt", False)]

    def test_size_change_is_modify(self):
        old = {"a.txt": self._make_file(size=10)}
        new = {"a.txt": self._make_file(size=20)}
        _, modified, _ = _diff_for_changes(old, new)
        assert modified == [("a.txt", False)]

    def test_mtime_change_is_modify(self):
        old = {"a.txt": self._make_file(mtime=100.0)}
        new = {"a.txt": self._make_file(mtime=200.0)}
        _, modified, _ = _diff_for_changes(old, new)
        assert modified == [("a.txt", False)]


@pytest.mark.asyncio
async def test_run_once_dispatches_synthetic_event(tmp_path):
    # Create one file and seed the rescanner with that manifest.
    (tmp_path / "a.txt").write_text("hello")
    seen = []

    def cb(event_type, rel_path, is_dir):
        seen.append((event_type, rel_path, is_dir))

    loop = asyncio.get_event_loop()
    rs = PeriodicRescanner(str(tmp_path), cb, loop, interval_s=999)
    rs.seed_known(scan_directory(str(tmp_path)))
    # Now create a new file outside the seeded manifest; run_once should fire.
    (tmp_path / "new.txt").write_text("surprise")
    await rs.run_once(trigger="test")
    assert ("create", "new.txt", False) in seen


@pytest.mark.asyncio
async def test_run_once_detects_deletion(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    seen = []

    def cb(event_type, rel_path, is_dir):
        seen.append((event_type, rel_path, is_dir))

    loop = asyncio.get_event_loop()
    rs = PeriodicRescanner(str(tmp_path), cb, loop, interval_s=999)
    rs.seed_known(scan_directory(str(tmp_path)))
    (tmp_path / "a.txt").unlink()
    await rs.run_once(trigger="test")
    assert ("delete", "a.txt", False) in seen


@pytest.mark.asyncio
async def test_async_callback_is_awaited(tmp_path):
    (tmp_path / "x.txt").write_text("x")
    seen = []

    async def cb(event_type, rel_path, is_dir):
        seen.append((event_type, rel_path, is_dir))

    loop = asyncio.get_event_loop()
    rs = PeriodicRescanner(str(tmp_path), cb, loop, interval_s=999)
    rs.seed_known(scan_directory(str(tmp_path)))
    (tmp_path / "y.txt").write_text("y")
    await rs.run_once(trigger="test")
    assert ("create", "y.txt", False) in seen
