import hashlib
import os

import pytest

from orcasync.staging import StagingFile, staging_dir, clean_staging
from orcasync.sync_engine import BLOCK_SIZE


def _hash(data):
    return hashlib.sha256(data).hexdigest()


class TestStagingFile:
    def test_write_and_commit_creates_target(self, tmp_path):
        data = b"hello staging"
        st = StagingFile(str(tmp_path), "out.bin", expected_size=len(data))
        assert st.write_block(0, data, expected_hash=_hash(data))
        st.commit()
        assert (tmp_path / "out.bin").read_bytes() == data

    def test_partial_file_lives_in_state_dir(self, tmp_path):
        st = StagingFile(str(tmp_path), "out.bin", expected_size=4)
        # The .partial file should sit under .orcasync/staging/, not at root.
        assert os.path.isdir(staging_dir(str(tmp_path)))
        assert not (tmp_path / "out.bin").exists()
        st.write_block(0, b"abcd", expected_hash=_hash(b"abcd"))
        st.commit()

    def test_hash_mismatch_rejects_block(self, tmp_path):
        st = StagingFile(str(tmp_path), "bad.bin", expected_size=4)
        wrong_hash = _hash(b"different")
        assert not st.write_block(0, b"abcd", expected_hash=wrong_hash)
        # Aborting cleans up the partial file.
        st.abort(reason="test")
        assert not (tmp_path / "bad.bin").exists()
        # Staging directory should have no .partial files left.
        leftovers = [n for n in os.listdir(staging_dir(str(tmp_path)))
                     if n.endswith(".partial")]
        assert leftovers == []

    def test_no_expected_hash_skips_verification(self, tmp_path):
        st = StagingFile(str(tmp_path), "x.bin", expected_size=2)
        # Without an expected hash the writer trusts the payload.
        assert st.write_block(0, b"ok")
        st.commit()
        assert (tmp_path / "x.bin").read_bytes() == b"ok"

    def test_multi_block_atomic_rename(self, tmp_path):
        b0 = b"\x00" * BLOCK_SIZE
        b1 = b"\x11" * 100
        size = BLOCK_SIZE + 100
        st = StagingFile(str(tmp_path), "multi.bin", expected_size=size)
        assert st.write_block(0, b0, expected_hash=_hash(b0))
        assert st.write_block(1, b1, expected_hash=_hash(b1))
        st.commit()
        data = (tmp_path / "multi.bin").read_bytes()
        assert len(data) == size
        assert data[:BLOCK_SIZE] == b0
        assert data[BLOCK_SIZE:] == b1

    def test_seeds_from_existing_target(self, tmp_path):
        # Existing 2-block file; we'll only overwrite block 0 in staging
        # and expect block 1 to be preserved via seeding.
        b0_old = b"A" * BLOCK_SIZE
        b1 = b"B" * 100
        (tmp_path / "f.bin").write_bytes(b0_old + b1)
        b0_new = b"X" * BLOCK_SIZE
        st = StagingFile(
            str(tmp_path), "f.bin", expected_size=BLOCK_SIZE + 100
        )
        assert st.write_block(0, b0_new, expected_hash=_hash(b0_new))
        st.commit()
        data = (tmp_path / "f.bin").read_bytes()
        assert data[:BLOCK_SIZE] == b0_new
        assert data[BLOCK_SIZE:] == b1

    def test_truncates_to_expected_size(self, tmp_path):
        # Existing larger file should be truncated to expected_size on commit.
        (tmp_path / "f.bin").write_bytes(b"X" * 500)
        st = StagingFile(str(tmp_path), "f.bin", expected_size=10)
        st.write_block(0, b"0123456789")
        st.commit()
        assert (tmp_path / "f.bin").read_bytes() == b"0123456789"


class TestCleanStaging:
    def test_removes_stray_partial_files(self, tmp_path):
        d = staging_dir(str(tmp_path))
        os.makedirs(d)
        (tmp_path / ".orcasync" / "staging" / "stale1.partial").write_bytes(b"x")
        (tmp_path / ".orcasync" / "staging" / "stale2.partial").write_bytes(b"y")
        clean_staging(str(tmp_path))
        leftovers = [n for n in os.listdir(d) if n.endswith(".partial")]
        assert leftovers == []

    def test_safe_when_no_state_dir(self, tmp_path):
        # Should be a no-op, not raise.
        clean_staging(str(tmp_path))


class TestStateDirIgnored:
    def test_scan_skips_orcasync_dir(self, tmp_path):
        from orcasync.sync_engine import scan_directory
        (tmp_path / "real.txt").write_bytes(b"hi")
        os.makedirs(tmp_path / ".orcasync" / "staging")
        (tmp_path / ".orcasync" / "staging" / "ghost.partial").write_bytes(b"x")
        manifest = scan_directory(str(tmp_path))
        assert "real.txt" in manifest
        # Even without a gitignore matcher, .orcasync/ must not appear.
        assert not any(p.startswith(".orcasync") for p in manifest)
