"""Periodic rescanner — the safety net for missed filesystem events.

Even with a watcher running, events can be lost: inotify queues can
overflow, network/virtual filesystems may not emit them, the process
may be paused or restarted, mtimes can be set into the past by
`touch -d` or `git checkout`. The rescanner periodically re-scans the
local tree and compares to the manifest seen at the time of the last
sync; if it finds drift it pushes a synthetic local-change notification
through the same callback the watcher uses.

This is the M1 stat-only variant: it trusts `(size, mtime)` to detect
change candidates and only recomputes block hashes for those files.
M2's persistent manifest cache will let us also detect bit-rot by
periodically re-hashing everything, but that's out of scope here.
"""

import asyncio
import logging
import os
import time

from .logging_util import log_event
from .sync_engine import scan_directory, normalize_path, STATE_DIR

logger = logging.getLogger("orcasync.rescan")

DEFAULT_INTERVAL_S = 600  # 10 minutes


class PeriodicRescanner:
    """Runs an incremental scan every `interval_s` seconds.

    `on_change(event_type, rel_path, is_dir)` is called for each
    detected discrepancy, mirroring the FileWatcher callback signature
    so the session code path is unchanged.
    """

    def __init__(
        self,
        root_path,
        on_change,
        loop,
        *,
        interval_s=DEFAULT_INTERVAL_S,
        gitignore_matcher=None,
    ):
        self.root_path = os.path.abspath(root_path)
        self.on_change = on_change
        self.loop = loop
        self.interval_s = interval_s
        self.gitignore_matcher = gitignore_matcher
        self._task = None
        self._stopping = asyncio.Event()
        self._known = {}  # path -> manifest entry from last scan

    def seed_known(self, manifest):
        """Initialize the baseline with an existing manifest (e.g. the
        manifest computed during initial sync)."""
        self._known = dict(manifest)

    def start(self):
        if self._task is None:
            self._task = self.loop.create_task(self._loop())
            log_event(
                logger, logging.INFO, "rescanner.started",
                root=self.root_path, interval_s=self.interval_s,
            )

    def stop(self):
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
            log_event(logger, logging.INFO, "rescanner.stopped", root=self.root_path)

    async def _loop(self):
        try:
            while not self._stopping.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.interval_s
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                await self.run_once(trigger="timer")
        except asyncio.CancelledError:
            return

    async def run_once(self, *, trigger="manual"):
        """One pass: scan, diff against `_known`, dispatch synthetic events."""
        started = time.time()
        log_event(
            logger, logging.INFO, "scan.start",
            type="incremental", trigger=trigger, root=self.root_path,
        )
        current = scan_directory(
            self.root_path,
            gitignore_matcher=self.gitignore_matcher,
            known_manifest=self._known,
        )
        added, modified, deleted = _diff_for_changes(self._known, current)
        for path, is_dir in deleted:
            await self._fire("delete", path, is_dir)
        for path, is_dir in added:
            await self._fire("create", path, is_dir)
        for path, is_dir in modified:
            await self._fire("modify", path, is_dir)
        self._known = current
        duration_ms = int((time.time() - started) * 1000)
        log_event(
            logger, logging.INFO, "scan.done",
            type="incremental", trigger=trigger,
            entries=len(current),
            added=len(added), modified=len(modified), deleted=len(deleted),
            duration_ms=duration_ms,
        )

    async def _fire(self, event_type, rel_path, is_dir):
        try:
            res = self.on_change(event_type, rel_path, is_dir)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            log_event(
                logger, logging.ERROR, "rescanner.dispatch_error",
                path=rel_path, event=event_type, error=str(e),
            )


def _diff_for_changes(old, new):
    """Return (added, modified, deleted) lists of `(path, is_dir)` tuples.

    A file counts as "modified" when its `(size, mtime)` or its block
    hashes diverge from the previous snapshot. Directories only ever
    appear in added/deleted (their content changes are picked up via
    the files inside them).
    """
    added, modified, deleted = [], [], []
    for path, info in new.items():
        prev = old.get(path)
        if prev is None:
            added.append((path, bool(info.get("is_dir"))))
            continue
        if info.get("is_dir") or prev.get("is_dir"):
            continue
        if info.get("size") != prev.get("size") or info.get("mtime") != prev.get("mtime"):
            modified.append((path, False))
            continue
        # mtime+size match — trust the cache and skip the hash compare.
    for path, prev in old.items():
        if path not in new:
            deleted.append((path, bool(prev.get("is_dir"))))
    return added, modified, deleted
