import hashlib
import json
import logging
import os
import sqlite3
import time

from .logging_util import log_event
from .sync_engine import STATE_DIR

logger = logging.getLogger("orcasync.manifest_db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT    PRIMARY KEY,
    is_dir      INTEGER NOT NULL,
    size        INTEGER,
    mtime       REAL,
    blocks_json TEXT,
    updated_at  REAL    NOT NULL
);
"""


def db_path_for(root_path, state_dir=None):
    """Return the manifest DB path for *root_path*.

    With --state-dir, uses a 12-char hash of the root path as a subdirectory
    so multiple roots share the same external state dir without collision.
    """
    if state_dir:
        h = hashlib.sha256(os.path.abspath(root_path).encode()).hexdigest()[:12]
        base = os.path.join(state_dir, h)
    else:
        base = os.path.join(root_path, STATE_DIR)
    return os.path.join(base, "manifest.db")


class ManifestDB:
    """Thin SQLite wrapper for persisting file manifests between runs.

    Allows scan_directory to skip re-hashing files whose (size, mtime) haven't
    changed since the last session — the same technique Syncthing uses with
    its LevelDB index to make rescans stat-only for unchanged files.
    """

    def __init__(self, path):
        self._path = path
        self._conn = None

    def open(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        log_event(logger, logging.DEBUG, "manifest_db.opened", path=self._path)

    def load(self):
        """Return all persisted entries as a manifest dict."""
        rows = self._conn.execute(
            "SELECT path, is_dir, size, mtime, blocks_json FROM files"
        ).fetchall()
        manifest = {}
        for path, is_dir, size, mtime, blocks_json in rows:
            if is_dir:
                manifest[path] = {"path": path, "is_dir": True, "mtime": mtime}
            else:
                blocks = json.loads(blocks_json) if blocks_json else []
                manifest[path] = {
                    "path": path, "is_dir": False,
                    "size": size, "mtime": mtime, "blocks": blocks,
                }
        log_event(logger, logging.DEBUG, "manifest_db.loaded",
                  path=self._path, entries=len(manifest))
        return manifest

    def save_many(self, manifest):
        """Upsert all entries that have confirmed block hashes (blocks != None)."""
        now = time.time()
        rows = []
        for path, info in manifest.items():
            if not info.get("is_dir") and info.get("blocks") is None:
                continue  # skip mtime-only entries not yet hashed
            if info.get("is_dir"):
                rows.append((path, 1, None, info.get("mtime"), None, now))
            else:
                rows.append((
                    path, 0, info.get("size"), info.get("mtime"),
                    json.dumps(info["blocks"]), now,
                ))
        if rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO files "
                "(path, is_dir, size, mtime, blocks_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        log_event(logger, logging.DEBUG, "manifest_db.saved",
                  path=self._path, entries=len(rows))

    def delete_many(self, paths):
        """Remove DB entries for deleted paths."""
        if paths:
            self._conn.executemany(
                "DELETE FROM files WHERE path = ?", [(p,) for p in paths]
            )
            self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            log_event(logger, logging.DEBUG, "manifest_db.closed", path=self._path)
