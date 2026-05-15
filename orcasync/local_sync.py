import os
import time
import asyncio
import logging

from .sync_engine import (
    scan_directory,
    diff_manifests,
    compute_file_blocks,
    read_block,
    delete_path,
    ensure_dir,
    BLOCK_SIZE,
)
from .staging import StagingFile, clean_staging
from .watcher import FileWatcher
from .gitignore import GitIgnoreMatcher
from .logging_util import log_event

logger = logging.getLogger("orcasync.local")


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
        clean_staging(self.src_path)
        clean_staging(self.dst_path)
        log_event(logger, logging.INFO, "scan.start", root=self.src_path, role="src")
        src_manifest = scan_directory(self.src_path, gitignore_matcher=self._src_gitignore)
        log_event(
            logger, logging.INFO, "scan.done",
            root=self.src_path, role="src", entries=len(src_manifest),
        )
        log_event(logger, logging.INFO, "scan.start", root=self.dst_path, role="dst")
        dst_manifest = scan_directory(self.dst_path, gitignore_matcher=self._dst_gitignore)
        log_event(
            logger, logging.INFO, "scan.done",
            root=self.dst_path, role="dst", entries=len(dst_manifest),
        )

        # Sync dst -> src
        src_needs = diff_manifests(src_manifest, dst_manifest)
        for need in src_needs:
            if need.get("is_dir"):
                ensure_dir(self.src_path, need["path"])
                log_event(logger, logging.INFO, "dir.created", root=self.src_path, path=need["path"])
            else:
                await self._pull_file(self.dst_path, self.src_path, need, dst_manifest)

        # Sync src -> dst
        dst_needs = diff_manifests(dst_manifest, src_manifest)
        for need in dst_needs:
            if need.get("is_dir"):
                ensure_dir(self.dst_path, need["path"])
                log_event(logger, logging.INFO, "dir.created", root=self.dst_path, path=need["path"])
            else:
                await self._pull_file(self.src_path, self.dst_path, need, src_manifest)

        log_event(logger, logging.INFO, "sync.initial_done")

    async def _pull_file(self, from_root, to_root, need, from_manifest):
        path = need["path"]
        indices = need.get("block_indices")
        info = from_manifest.get(path, {})
        expected_size = info.get("size", 0) or 0
        hashes = {b["index"]: b["hash"] for b in info.get("blocks", [])}

        st = StagingFile(to_root, path, expected_size=expected_size)
        try:
            if indices is None:
                # All blocks need to be copied (file is new or fully changed).
                indices = sorted(hashes.keys())
            for idx in indices:
                block = read_block(from_root, path, idx)
                if block is None:
                    continue
                st.write_block(idx, block, expected_hash=hashes.get(idx))
            st.commit()
        except Exception as e:
            st.abort(reason=str(e))
            raise

        self._synced_files[path] = time.time()
        log_event(
            logger, logging.INFO, "sync.done",
            direction=f"{os.path.basename(from_root)}->{os.path.basename(to_root)}",
            path=path, size=expected_size, blocks=len(indices),
        )

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
            log_event(logger, logging.INFO, "sync.delete", path=rel_path, to=to_root)
        elif event_type == "create" and is_dir:
            ensure_dir(to_root, rel_path)
            log_event(logger, logging.INFO, "sync.mkdir", path=rel_path, to=to_root)
        elif event_type in ("create", "modify") and not is_dir:
            fpath = os.path.join(from_root, rel_path.replace("/", os.sep))
            if os.path.isfile(fpath):
                blocks = compute_file_blocks(fpath)
                size = os.path.getsize(fpath)
                st = StagingFile(to_root, rel_path, expected_size=size)
                try:
                    for b in blocks:
                        idx = b["index"]
                        block = read_block(from_root, rel_path, idx)
                        if block is None:
                            continue
                        st.write_block(idx, block, expected_hash=b["hash"])
                    st.commit()
                except Exception as e:
                    st.abort(reason=str(e))
                    raise
                self._synced_files[rel_path] = time.time()
                log_event(
                    logger, logging.INFO, "sync.done",
                    direction=f"{os.path.basename(from_root)}->{os.path.basename(to_root)}",
                    path=rel_path, size=size, blocks=len(blocks),
                )

    def stop(self):
        self._running = False
        if self._src_watcher:
            self._src_watcher.stop()
        if self._dst_watcher:
            self._dst_watcher.stop()
