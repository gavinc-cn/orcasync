import os
import hashlib
import tempfile

import pytest

from orcasync.sync_engine import (
    BLOCK_SIZE,
    compute_file_blocks,
    scan_directory,
    diff_manifests,
    read_block,
    write_blocks,
    delete_path,
    ensure_parent_dir,
    _same_blocks,
)


class TestComputeFileBlocks:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert compute_file_blocks(str(f)) == []

    def test_small_file(self, tmp_path):
        data = b"hello world"
        f = tmp_path / "small.bin"
        f.write_bytes(data)
        blocks = compute_file_blocks(str(f))
        assert len(blocks) == 1
        assert blocks[0]["index"] == 0
        assert blocks[0]["size"] == len(data)
        assert blocks[0]["hash"] == hashlib.sha256(data).hexdigest()

    def test_exact_block_size(self, tmp_path):
        data = b"\x42" * BLOCK_SIZE
        f = tmp_path / "exact.bin"
        f.write_bytes(data)
        blocks = compute_file_blocks(str(f))
        assert len(blocks) == 1
        assert blocks[0]["size"] == BLOCK_SIZE

    def test_multiple_blocks(self, tmp_path):
        data = b"\x00" * BLOCK_SIZE + b"\x01" * BLOCK_SIZE + b"tail"
        f = tmp_path / "multi.bin"
        f.write_bytes(data)
        blocks = compute_file_blocks(str(f))
        assert len(blocks) == 3
        assert blocks[0]["index"] == 0
        assert blocks[1]["index"] == 1
        assert blocks[2]["index"] == 2
        assert blocks[0]["size"] == BLOCK_SIZE
        assert blocks[1]["size"] == BLOCK_SIZE
        assert blocks[2]["size"] == 4

    def test_nonexistent_file(self):
        assert compute_file_blocks("/nonexistent/path/file.bin") == []


class TestScanDirectory:
    def test_empty_directory(self, tmp_path):
        manifest = scan_directory(str(tmp_path))
        assert manifest == {}

    def test_single_file(self, tmp_path):
        (tmp_path / "test.txt").write_bytes(b"content")
        manifest = scan_directory(str(tmp_path))
        assert "test.txt" in manifest
        info = manifest["test.txt"]
        assert info["size"] == 7
        assert info["is_dir"] is False
        assert len(info["blocks"]) == 1

    def test_nested_files(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.txt").write_bytes(b"aaa")
        (sub / "b.txt").write_bytes(b"bbb")
        manifest = scan_directory(str(tmp_path))
        assert os.path.join("sub", "a.txt") in manifest
        assert os.path.join("sub", "b.txt") in manifest

    def test_deeply_nested(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_bytes(b"deep")
        manifest = scan_directory(str(tmp_path))
        key = os.path.join("a", "b", "c", "deep.txt")
        assert key in manifest


class TestDiffManifests:
    def _make_entry(self, path, mtime=100.0, blocks=None, is_dir=False):
        if blocks is None:
            blocks = [{"index": 0, "size": 10, "hash": "abc"}]
        return {
            "path": path,
            "size": 10,
            "mtime": mtime,
            "is_dir": is_dir,
            "blocks": blocks,
        }

    def test_remote_file_missing_locally(self):
        local = {}
        remote = {"foo.txt": self._make_entry("foo.txt")}
        needs = diff_manifests(local, remote)
        assert len(needs) == 1
        assert needs[0]["path"] == "foo.txt"
        assert needs[0]["block_indices"] is None

    def test_identical_blocks_no_pull(self):
        entry = self._make_entry("same.txt")
        local = {"same.txt": entry}
        remote = {"same.txt": entry}
        assert diff_manifests(local, remote) == []

    def test_older_remote_skipped(self):
        local = {"old.txt": self._make_entry("old.txt", mtime=200.0)}
        remote = {"old.txt": self._make_entry("old.txt", mtime=100.0)}
        assert diff_manifests(local, remote) == []

    def test_newer_remote_changed_blocks(self):
        blocks_a = [{"index": 0, "size": 10, "hash": "hash_a"}]
        blocks_b = [{"index": 0, "size": 10, "hash": "hash_b"}]
        local = {"f.txt": self._make_entry("f.txt", mtime=100.0, blocks=blocks_a)}
        remote = {"f.txt": self._make_entry("f.txt", mtime=200.0, blocks=blocks_b)}
        needs = diff_manifests(local, remote)
        assert len(needs) == 1
        assert needs[0]["block_indices"] == [0]

    def test_dir_entries_skipped(self):
        local = {}
        remote = {"adir": {"path": "adir", "is_dir": True, "blocks": []}}
        assert diff_manifests(local, remote) == []

    def test_partial_block_change(self):
        blocks_local = [
            {"index": 0, "size": 10, "hash": "same"},
            {"index": 1, "size": 10, "hash": "old"},
            {"index": 2, "size": 10, "hash": "same"},
        ]
        blocks_remote = [
            {"index": 0, "size": 10, "hash": "same"},
            {"index": 1, "size": 10, "hash": "new"},
            {"index": 2, "size": 10, "hash": "same"},
        ]
        local = {"f.txt": self._make_entry("f.txt", mtime=100.0, blocks=blocks_local)}
        remote = {"f.txt": self._make_entry("f.txt", mtime=200.0, blocks=blocks_remote)}
        needs = diff_manifests(local, remote)
        assert len(needs) == 1
        assert needs[0]["block_indices"] == [1]


class TestSameBlocks:
    def test_equal(self):
        a = [{"index": 0, "hash": "x"}]
        b = [{"index": 0, "hash": "x"}]
        assert _same_blocks(a, b) is True

    def test_different_length(self):
        a = [{"index": 0, "hash": "x"}]
        b = [{"index": 0, "hash": "x"}, {"index": 1, "hash": "y"}]
        assert _same_blocks(a, b) is False

    def test_different_hash(self):
        a = [{"index": 0, "hash": "x"}]
        b = [{"index": 0, "hash": "y"}]
        assert _same_blocks(a, b) is False


class TestReadBlock:
    def test_read_first_block(self, tmp_path):
        data = b"A" * BLOCK_SIZE + b"B" * 100
        f = tmp_path / "test.bin"
        f.write_bytes(data)
        block = read_block(str(tmp_path), "test.bin", 0)
        assert block == b"A" * BLOCK_SIZE

    def test_read_second_block(self, tmp_path):
        data = b"A" * BLOCK_SIZE + b"B" * 100
        f = tmp_path / "test.bin"
        f.write_bytes(data)
        block = read_block(str(tmp_path), "test.bin", 1)
        assert block == b"B" * 100

    def test_read_nonexistent_file(self, tmp_path):
        assert read_block(str(tmp_path), "nope.bin", 0) is None


class TestWriteBlocks:
    def test_write_and_read_roundtrip(self, tmp_path):
        data = b"hello block data"
        write_blocks(str(tmp_path), "out.bin", [(0, data)], expected_size=len(data))
        result = read_block(str(tmp_path), "out.bin", 0)
        assert result == data

    def test_write_multiple_blocks(self, tmp_path):
        b0 = b"\x00" * BLOCK_SIZE
        b1 = b"\x11" * 50
        write_blocks(
            str(tmp_path),
            "multi.bin",
            [(0, b0), (1, b1)],
            expected_size=BLOCK_SIZE + 50,
        )
        assert read_block(str(tmp_path), "multi.bin", 0) == b0
        assert read_block(str(tmp_path), "multi.bin", 1) == b1

    def test_truncate_to_expected_size(self, tmp_path):
        f = tmp_path / "trunc.bin"
        f.write_bytes(b"\x00" * 200)
        write_blocks(
            str(tmp_path),
            "trunc.bin",
            [(0, b"\xff" * 50)],
            expected_size=50,
        )
        assert os.path.getsize(str(f)) == 50

    def test_creates_parent_dirs(self, tmp_path):
        write_blocks(str(tmp_path), os.path.join("a", "b", "file.bin"), [(0, b"x")])
        assert (tmp_path / "a" / "b" / "file.bin").exists()


class TestDeletePath:
    def test_delete_file(self, tmp_path):
        f = tmp_path / "del.txt"
        f.write_text("bye")
        delete_path(str(tmp_path), "del.txt")
        assert not f.exists()

    def test_delete_directory(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        (d / "inner.txt").write_text("inner")
        delete_path(str(tmp_path), "subdir")
        assert not d.exists()

    def test_delete_nonexistent(self, tmp_path):
        delete_path(str(tmp_path), "ghost.txt")


class TestEnsureParentDir:
    def test_creates_parent(self, tmp_path):
        ensure_parent_dir(str(tmp_path), os.path.join("a", "b", "file.txt"))
        assert (tmp_path / "a" / "b").is_dir()

    def test_existing_parent(self, tmp_path):
        (tmp_path / "a").mkdir()
        ensure_parent_dir(str(tmp_path), os.path.join("a", "file.txt"))
        assert (tmp_path / "a").is_dir()
