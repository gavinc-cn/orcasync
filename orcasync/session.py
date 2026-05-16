import os
import time
import asyncio
import logging

from .protocol import send_message, recv_message
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
from .conflict import detect_conflict, preserve_local_as_conflict
from .rescanner import PeriodicRescanner, DEFAULT_INTERVAL_S as RESCAN_INTERVAL_S

logger = logging.getLogger("orcasync.session")

MAX_BLOCK_RETRIES = 3


class SyncSession:
    def __init__(self, root_path, reader, writer, loop, use_gitignore=True,
                 rescan_interval_s=RESCAN_INTERVAL_S, state_dir=None):
        self.root_path = os.path.abspath(root_path)
        self.reader = reader
        self.writer = writer
        self.loop = loop
        self._send_lock = asyncio.Lock()
        self._running = True
        self._recv_task = None
        self._watcher = None
        self._rescanner = None
        self._rescan_interval_s = rescan_interval_s
        self._state_dir = os.path.abspath(state_dir) if state_dir else None
        self._gitignore_matcher = GitIgnoreMatcher(root_path) if use_gitignore else None

        self.local_manifest = {}
        self._remote_manifest = None
        self._manifest_event = asyncio.Event()
        # path -> StagingFile for in-flight transfers (streaming write)
        self._staging = {}
        # path -> {index: retry_count} for block-level retry tracking
        self._block_retries = {}
        # path -> expected block hashes from manifest, used for verification
        self._expected_block_hashes = {}
        self._pending_transfers = set()
        self._received_sync_done = False
        self._sync_event = asyncio.Event()
        self._initial_sync_done = False
        self._synced_files = {}
        clean_staging(self.root_path, self._state_dir)

    async def send(self, msg_type, data=None, payload=b""):
        async with self._send_lock:
            try:
                await send_message(self.writer, msg_type, data, payload)
            except (ConnectionError, OSError):
                self._running = False

    async def run_as_client(self):
        try:
            self._recv_task = asyncio.create_task(self._recv_loop())
            log_event(logger, logging.INFO, "scan.start", role="client", root=self.root_path)
            self.local_manifest = scan_directory(self.root_path, gitignore_matcher=self._gitignore_matcher)
            log_event(logger, logging.INFO, "scan.done", role="client", entries=len(self.local_manifest))
            await self.send("manifest", {"files": self.local_manifest})
            await self._manifest_event.wait()
            log_event(
                logger, logging.INFO, "manifest.received",
                role="client", entries=len(self._remote_manifest),
            )
            await self._request_needed()
            await self._sync_event.wait()
            self._initial_sync_done = True
            self._start_watcher()
            log_event(logger, logging.INFO, "sync.initial_done", role="client")
            await self._recv_task
        except Exception as e:
            log_event(logger, logging.ERROR, "session.error", role="client", error=str(e))
        finally:
            self._cleanup()

    async def run_as_server(self):
        try:
            self._recv_task = asyncio.create_task(self._recv_loop())
            await self._manifest_event.wait()
            log_event(
                logger, logging.INFO, "manifest.received",
                role="server", entries=len(self._remote_manifest),
            )
            log_event(logger, logging.INFO, "scan.start", role="server", root=self.root_path)
            self.local_manifest = scan_directory(self.root_path, gitignore_matcher=self._gitignore_matcher)
            log_event(logger, logging.INFO, "scan.done", role="server", entries=len(self.local_manifest))
            await self.send("manifest", {"files": self.local_manifest})
            await self._request_needed()
            await self._sync_event.wait()
            self._initial_sync_done = True
            self._start_watcher()
            log_event(logger, logging.INFO, "sync.initial_done", role="server")
            await self._recv_task
        except Exception as e:
            log_event(logger, logging.ERROR, "session.error", role="server", error=str(e))
        finally:
            self._cleanup()

    async def _request_needed(self):
        needs = diff_manifests(self.local_manifest, self._remote_manifest)
        file_needs = [n for n in needs if not n.get("is_dir")]
        dir_needs = [n for n in needs if n.get("is_dir")]
        self._pending_transfers = {n["path"] for n in file_needs}
        total_bytes = sum(
            self._remote_manifest.get(n["path"], {}).get("size", 0) or 0
            for n in file_needs
        )
        log_event(
            logger,
            logging.INFO,
            "diff.done",
            files_to_pull=len(file_needs),
            dirs_to_pull=len(dir_needs),
            bytes_to_transfer=total_bytes,
        )
        # Create directories immediately (no need to request blocks)
        for need in dir_needs:
            ensure_dir(self.root_path, need["path"])
            log_event(logger, logging.INFO, "dir.created", path=need["path"])
        for need in file_needs:
            path = need["path"]
            remote_info = self._remote_manifest.get(path, {})
            local_info = self.local_manifest.get(path)
            # Concurrent edit on both sides → preserve our local copy as
            # <name>.sync-conflict-* before we overwrite it with the
            # remote version. The conflict file becomes a regular file
            # in subsequent scans and propagates to the peer.
            if detect_conflict(local_info, remote_info):
                log_event(
                    logger, logging.WARNING, "conflict.detected",
                    path=path,
                    local_mtime=local_info.get("mtime"),
                    remote_mtime=remote_info.get("mtime"),
                )
                preserve_local_as_conflict(self.root_path, path)
            # Cache expected per-block hashes from the remote manifest so we
            # can verify each incoming block payload end-to-end.
            blocks = remote_info.get("blocks", []) or []
            self._expected_block_hashes[path] = {
                b["index"]: b["hash"] for b in blocks
            }
            # Pre-create the staging file so we can stream blocks straight
            # to disk instead of buffering the whole file in memory.
            expected_size = remote_info.get("size", 0) or 0
            self._staging[path] = StagingFile(
                self.root_path, path, expected_size=expected_size,
                state_dir=self._state_dir,
            )
            indices = need["block_indices"]
            await self.send(
                "request_blocks",
                {"path": path, "indices": indices if indices else "all"},
            )
        if not file_needs and not dir_needs:
            await self.send("sync_done", {})
            self._check_sync_complete()

    async def _recv_loop(self):
        while self._running:
            try:
                msg_type, data, payload = await recv_message(self.reader)
            except (
                asyncio.IncompleteReadError,
                ConnectionError,
                OSError,
            ):
                break
            except Exception:
                break
            handler = getattr(self, f"_handle_{msg_type}", None)
            if handler:
                try:
                    await handler(data, payload)
                except Exception as e:
                    log_event(
                        logger, logging.ERROR, "handler.error",
                        msg_type=msg_type, error=str(e),
                    )
        self._running = False

    async def _handle_manifest(self, data, _payload):
        self._remote_manifest = data.get("files", {})
        self._manifest_event.set()

    async def _handle_request_blocks(self, data, _payload):
        path = data["path"]
        indices = data.get("indices", "all")
        filepath = os.path.join(self.root_path, path)

        if not os.path.isfile(filepath):
            await self.send("transfer_done", {"path": path, "size": 0})
            return

        # Compute hashes so the receiver can verify each block end-to-end.
        all_blocks = compute_file_blocks(filepath)
        hash_by_index = {b["index"]: b["hash"] for b in all_blocks}

        if indices == "all":
            indices = [b["index"] for b in all_blocks]

        file_size = os.path.getsize(filepath)

        for idx in indices:
            block = read_block(self.root_path, path, idx)
            if block is not None:
                await self.send(
                    "block_data",
                    {"path": path, "index": idx, "hash": hash_by_index.get(idx)},
                    payload=block,
                )

        await self.send("transfer_done", {"path": path, "size": file_size})

    async def _handle_block_data(self, data, payload):
        path = data["path"]
        index = data["index"]
        block_hash = data.get("hash")
        # Fall back to the manifest-cached expected hash if the sender's
        # message didn't include one (legacy peer).
        if block_hash is None:
            block_hash = self._expected_block_hashes.get(path, {}).get(index)

        st = self._staging.get(path)
        if st is None:
            # Late delivery for a file we already gave up on; ignore.
            return

        if st.write_block(index, payload, expected_hash=block_hash):
            return

        # Hash mismatch: ask the sender to retransmit this single block,
        # up to MAX_BLOCK_RETRIES times.
        retries = self._block_retries.setdefault(path, {})
        retries[index] = retries.get(index, 0) + 1
        if retries[index] > MAX_BLOCK_RETRIES:
            log_event(
                logger,
                logging.ERROR,
                "block.gave_up",
                path=path,
                index=index,
                retries=retries[index],
            )
            st.abort(reason="block_hash_mismatch_exhausted")
            self._staging.pop(path, None)
            self._pending_transfers.discard(path)
            self._check_sync_complete()
            return
        log_event(
            logger,
            logging.WARNING,
            "block.retry",
            path=path,
            index=index,
            retry=retries[index],
        )
        await self.send("request_blocks", {"path": path, "indices": [index]})

    async def _handle_transfer_done(self, data, _payload):
        path = data["path"]
        size = data.get("size")
        st = self._staging.pop(path, None)
        self._expected_block_hashes.pop(path, None)
        self._block_retries.pop(path, None)
        if st is not None:
            # The receiver's expected_size came from the remote manifest at
            # request time; trust the sender's authoritative final size here.
            if size is not None:
                st.expected_size = size
            try:
                st.commit()
                self._synced_files[path] = time.time()
            except Exception as e:
                log_event(
                    logger, logging.ERROR, "transfer.commit_failed",
                    path=path, error=str(e),
                )
        elif size == 0:
            # Empty file: no block_data arrived, write directly via a
            # zero-byte StagingFile commit for consistency.
            empty = StagingFile(self.root_path, path, expected_size=0, state_dir=self._state_dir)
            try:
                empty.commit()
                self._synced_files[path] = time.time()
            except Exception as e:
                log_event(
                    logger, logging.ERROR, "transfer.commit_failed",
                    path=path, error=str(e),
                )
        self._pending_transfers.discard(path)
        if not self._pending_transfers and not self._initial_sync_done:
            await self.send("sync_done", {})
        self._check_sync_complete()

    async def _handle_sync_done(self, _data, _payload):
        self._received_sync_done = True
        self._check_sync_complete()

    def _check_sync_complete(self):
        if self._initial_sync_done:
            return
        if not self._pending_transfers and self._received_sync_done:
            self._sync_event.set()

    async def _handle_file_event(self, data, _payload):
        if not self._initial_sync_done:
            return
        event = data["event"]
        path = data["path"]
        is_dir = data.get("is_dir", False)

        if event == "delete":
            self._synced_files[path] = time.time()
            delete_path(self.root_path, path)
            log_event(logger, logging.INFO, "remote.delete", path=path, is_dir=is_dir)
        elif event == "create" and is_dir:
            full = os.path.join(self.root_path, path)
            os.makedirs(full, exist_ok=True)
            log_event(logger, logging.INFO, "remote.mkdir", path=path)
        elif event in ("create", "modify") and not is_dir:
            remote_mtime = data.get("mtime", 0)
            fpath = os.path.join(self.root_path, path)
            if os.path.exists(fpath) and not os.path.isdir(fpath):
                local_mtime = os.path.getmtime(fpath)
                if local_mtime > remote_mtime:
                    log_event(
                        logger, logging.DEBUG, "remote.skipped_newer_local",
                        path=path, local_mtime=local_mtime, remote_mtime=remote_mtime,
                    )
                    return

            block_hashes = data.get("block_hashes", [])
            local_blocks = []
            if os.path.isfile(fpath):
                local_blocks = compute_file_blocks(fpath)

            local_hash_map = {b["index"]: b["hash"] for b in local_blocks}
            needed = [
                i
                for i, h in enumerate(block_hashes)
                if h != local_hash_map.get(i)
            ]
            expected_size = data.get("size")

            if needed:
                self._pending_transfers.add(path)
                self._expected_block_hashes[path] = {
                    i: h for i, h in enumerate(block_hashes)
                }
                # Staging seeds from the existing target file, so unchanged
                # blocks are preserved automatically; we only need to write
                # the blocks the remote will send us.
                self._staging[path] = StagingFile(
                    self.root_path, path, expected_size=expected_size,
                    state_dir=self._state_dir,
                )
                await self.send(
                    "request_blocks",
                    {"path": path, "indices": needed},
                )
            else:
                self._synced_files[path] = time.time()
                if expected_size is not None:
                    if not os.path.exists(fpath):
                        open(fpath, "wb").close()
                    if os.path.isfile(fpath):
                        with open(fpath, "r+b") as f:
                            f.truncate(expected_size)
                    log_event(
                        logger, logging.INFO, "remote.already_in_sync",
                        path=path, size=expected_size,
                    )

    def _start_watcher(self):
        self._watcher = FileWatcher(
            self.root_path, self._on_file_change, self.loop,
            gitignore_matcher=self._gitignore_matcher,
        )
        self._watcher.start()
        log_event(logger, logging.INFO, "watcher.started", root=self.root_path)
        if self._rescan_interval_s and self._rescan_interval_s > 0:
            self._rescanner = PeriodicRescanner(
                self.root_path,
                self._on_file_change,
                self.loop,
                interval_s=self._rescan_interval_s,
                gitignore_matcher=self._gitignore_matcher,
            )
            self._rescanner.seed_known(self.local_manifest)
            self._rescanner.start()

    async def _on_file_change(self, event_type, rel_path, is_dir):
        if rel_path in self._synced_files:
            if time.time() - self._synced_files[rel_path] < 2.0:
                log_event(
                    logger, logging.DEBUG, "echo.suppressed",
                    path=rel_path, reason="apply_window",
                )
                return
            del self._synced_files[rel_path]

        if not self._running:
            return

        log_event(
            logger, logging.INFO, "local.change",
            event=event_type, path=rel_path, is_dir=is_dir,
        )

        if event_type == "delete":
            await self.send(
                "file_event",
                {"event": "delete", "path": rel_path, "is_dir": is_dir},
            )
        elif event_type == "create" and is_dir:
            await self.send(
                "file_event",
                {"event": "create", "path": rel_path, "is_dir": True},
            )
        else:
            fpath = os.path.join(self.root_path, rel_path)
            if os.path.isfile(fpath):
                blocks = compute_file_blocks(fpath)
                stat = os.stat(fpath)
                await self.send(
                    "file_event",
                    {
                        "event": event_type,
                        "path": rel_path,
                        "is_dir": False,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "block_hashes": [b["hash"] for b in blocks],
                    },
                )

    def _cleanup(self):
        self._running = False
        if self._rescanner:
            self._rescanner.stop()
        if self._watcher:
            self._watcher.stop()
        if not self.writer.is_closing():
            try:
                self.writer.close()
            except Exception:
                pass
        log_event(logger, logging.INFO, "session.closed")
