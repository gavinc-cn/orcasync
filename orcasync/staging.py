"""Streaming staging writer for incoming file transfers.

Blocks are written into <root>/.orcasync/staging/<token>.partial as they
arrive (no in-memory buffering of the full file). On transfer completion
the staged file is atomically renamed into place.

A per-block SHA-256 may be supplied by the sender; if so it is verified
before the block is written and a hash mismatch is reported back to the
caller so it can request a retransmit.
"""

import hashlib
import logging
import os
import time
import uuid

from .logging_util import log_event
from .sync_engine import BLOCK_SIZE, STATE_DIR, ensure_parent_dir

logger = logging.getLogger("orcasync.staging")


def staging_dir(root_path):
    return os.path.join(root_path, STATE_DIR, "staging")


def clean_staging(root_path):
    """Remove stray .partial files left by crashed sessions."""
    d = staging_dir(root_path)
    if not os.path.isdir(d):
        return
    removed = 0
    for name in os.listdir(d):
        if name.endswith(".partial"):
            try:
                os.remove(os.path.join(d, name))
                removed += 1
            except OSError:
                pass
    if removed:
        log_event(logger, logging.INFO, "staging.cleaned", removed=removed, dir=d)


class StagingFile:
    """One staging file = one in-progress file transfer.

    Usage:
        st = StagingFile(root, "a/b.txt", expected_size=4096)
        st.write_block(0, payload, expected_hash="ab12...")  # raises HashMismatch on bad data
        st.commit()  # atomic rename into place
        # or st.abort() to discard
    """

    def __init__(self, root_path, rel_path, expected_size=None, seed_from_existing=True):
        self.root_path = os.path.abspath(root_path)
        self.rel_path = rel_path
        self.expected_size = expected_size
        self._target = os.path.join(self.root_path, rel_path.replace("/", os.sep))
        self._token = uuid.uuid4().hex
        self._partial = os.path.join(
            staging_dir(self.root_path), f"{self._token}.partial"
        )
        os.makedirs(staging_dir(self.root_path), exist_ok=True)
        # Seed the staging file with the current target's contents so that
        # block-level delta updates only need to overwrite changed blocks
        # and unchanged regions are preserved automatically.
        seeded_bytes = 0
        if seed_from_existing and os.path.isfile(self._target):
            try:
                with open(self._target, "rb") as src, open(self._partial, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        seeded_bytes += len(chunk)
            except OSError:
                # If seeding fails, fall back to a fresh empty staging file.
                seeded_bytes = 0
        self._fh = open(self._partial, "r+b" if seeded_bytes else "wb")
        if seeded_bytes:
            self._fh.seek(0)
        self._bytes_written = 0
        self._blocks_written = 0
        self._seeded_bytes = seeded_bytes
        self._opened_at = time.time()
        log_event(
            logger,
            logging.DEBUG,
            "staging.open",
            path=rel_path,
            expected_size=expected_size,
            seeded_bytes=seeded_bytes,
            token=self._token,
        )

    def write_block(self, index, data, expected_hash=None):
        """Write one block. Returns True on success, False if hash mismatched.

        On mismatch the block is NOT written and the caller should request a
        retransmit. The staging file remains open and consistent.
        """
        if expected_hash is not None:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected_hash:
                log_event(
                    logger,
                    logging.WARNING,
                    "block.hash_mismatch",
                    path=self.rel_path,
                    index=index,
                    expected=expected_hash[:12],
                    got=actual[:12],
                    size=len(data),
                )
                return False
        self._fh.seek(index * BLOCK_SIZE)
        self._fh.write(data)
        self._bytes_written += len(data)
        self._blocks_written += 1
        log_event(
            logger,
            logging.DEBUG,
            "staging.block_written",
            path=self.rel_path,
            index=index,
            size=len(data),
            verified=expected_hash is not None,
        )
        return True

    def commit(self):
        """Truncate to expected size, close, and atomically rename into place."""
        try:
            if self.expected_size is not None:
                self._fh.truncate(self.expected_size)
            self._fh.flush()
            os.fsync(self._fh.fileno())
        finally:
            self._fh.close()
        ensure_parent_dir(self.root_path, self.rel_path)
        os.replace(self._partial, self._target)
        duration_ms = int((time.time() - self._opened_at) * 1000)
        log_event(
            logger,
            logging.INFO,
            "transfer.applied",
            path=self.rel_path,
            size=self.expected_size if self.expected_size is not None else self._bytes_written,
            blocks=self._blocks_written,
            duration_ms=duration_ms,
        )

    def abort(self, reason="unknown"):
        try:
            self._fh.close()
        except Exception:
            pass
        try:
            os.remove(self._partial)
        except OSError:
            pass
        log_event(
            logger,
            logging.WARNING,
            "transfer.aborted",
            path=self.rel_path,
            reason=reason,
            bytes_written=self._bytes_written,
        )
