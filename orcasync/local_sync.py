import os
import time
import asyncio
import logging

from .sync_engine import (
    scan_directory,
    diff_manifests,
    compute_file_blocks,
    read_block,
    write_blocks,
    delete_path,
    ensure_dir,
    BLOCK_SIZE,
)
from .watcher import FileWatcher
from .gitignore import GitIgnoreMatcher

logger = logging.getLogger("orcasync")


class LocalSyncSession:
    def __init__(self, src_path, dst_path, use_gitignore=True):
        self.src_path = os.path.abspath(src_path)
        self.dst_path = os.path.abspath(dst_path)
        self._running = True
        self._src_watcher = None
        self._dst_watcher = None
        self._synced_files = {}
        self._lock = asyncio.Lock()
        self._src_gitignore = GitIgnoreMatcher(self.src_path) if use_gitignore else None
        self._dst_gitignore = GitIgnoreMatcher(self.dst_path) if use_gitignore else None

    async def run(self):
        await self.run_initial_sync()
        self._start_watchers()
        logger.info("Local sync active: %s <-> %s", self.src_path, self.dst_path)
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def run_initial_sync(self):
        src_manifest = scan_directory(self.src_path, gitignore_matcher=self._src_gitignore)
        dst_manifest = scan_directory(self.dst_path, gitignore_matcher=self._dst_gitignore)

        # Sync dst -> src
        src_needs = diff_manifests(src_manifest, dst_manifest)
        for need in src_needs:
            if need.get("is_dir"):
                ensure_dir(self.src_path, need["path"])
                logger.info("Created dir in src: %s", need["path"])
            else:
                await self._pull_file(self.dst_path, self.src_path, need)

        # Sync src -> dst
        dst_needs = diff_manifests(dst_manifest, src_manifest)
        for need in dst_needs:
            if need.get("is_dir"):
                ensure_dir(self.dst_path, need["path"])
                logger.info("Created dir in dst: %s", need["path"])
            else:
                await self._pull_file(self.src_path, self.dst_path, need)

        logger.info("Initial sync complete")

    async def _pull_file(self, from_root, to_root, need):
        path = need["path"]
        indices = need.get("block_indices")

        if indices is None:
            # Full file copy
            filepath = os.path.join(from_root, path.replace("/", os.sep))
            with open(filepath, "rb") as f:
                data = f.read()
            write_blocks(to_root, path, [(0, data)], expected_size=len(data))
        else:
            # Block-level copy
            blocks = []
            for idx in indices:
                block = read_block(from_root, path, idx)
                if block is not None:
                    blocks.append((idx, block))
            write_blocks(to_root, path, blocks)

        self._synced_files[path] = time.time()
        logger.info("Synced: %s", path)

    def _start_watchers(self):
        loop = asyncio.get_running_loop()
        self._src_watcher = FileWatcher(
            self.src_path, self._on_src_change, loop,
            gitignore_matcher=self._src_gitignore,
        )
        self._dst_watcher = FileWatcher(
            self.dst_path, self._on_dst_change, loop,
            gitignore_matcher=self._dst_gitignore,
        )
        self._src_watcher.start()
        self._dst_watcher.start()

    async def _on_src_change(self, event_type, rel_path, is_dir):
        async with self._lock:
            await self._handle_change(self.src_path, self.dst_path, event_type, rel_path, is_dir)

    async def _on_dst_change(self, event_type, rel_path, is_dir):
        async with self._lock:
            await self._handle_change(self.dst_path, self.src_path, event_type, rel_path, is_dir)

    async def _handle_change(self, from_root, to_root, event_type, rel_path, is_dir):
        if rel_path in self._synced_files:
            if time.time() - self._synced_files[rel_path] < 2.0:
                return
            del self._synced_files[rel_path]

        if event_type == "delete":
            delete_path(to_root, rel_path)
            logger.info("Deleted: %s", rel_path)
        elif event_type == "create" and is_dir:
            ensure_dir(to_root, rel_path)
            logger.info("Created dir: %s", rel_path)
        elif event_type in ("create", "modify") and not is_dir:
            fpath = os.path.join(from_root, rel_path.replace("/", os.sep))
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                write_blocks(to_root, rel_path, [(0, data)], expected_size=len(data))
                self._synced_files[rel_path] = time.time()
                logger.info("Synced: %s", rel_path)

    def stop(self):
        self._running = False
        if self._src_watcher:
            self._src_watcher.stop()
        if self._dst_watcher:
            self._dst_watcher.stop()
