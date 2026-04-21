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
    write_blocks,
    delete_path,
    BLOCK_SIZE,
)
from .watcher import FileWatcher

logger = logging.getLogger("orcasync")


class SyncSession:
    def __init__(self, root_path, reader, writer, loop):
        self.root_path = os.path.abspath(root_path)
        self.reader = reader
        self.writer = writer
        self.loop = loop
        self._send_lock = asyncio.Lock()
        self._running = True
        self._recv_task = None
        self._watcher = None

        self.local_manifest = {}
        self._remote_manifest = None
        self._manifest_event = asyncio.Event()
        self._pending_blocks = {}
        self._pending_transfers = set()
        self._received_sync_done = False
        self._sync_event = asyncio.Event()
        self._initial_sync_done = False
        self._synced_files = {}

    async def send(self, msg_type, data=None, payload=b""):
        async with self._send_lock:
            try:
                await send_message(self.writer, msg_type, data, payload)
            except (ConnectionError, OSError):
                self._running = False

    async def run_as_client(self):
        try:
            self._recv_task = asyncio.create_task(self._recv_loop())
            self.local_manifest = scan_directory(self.root_path)
            logger.info("Local manifest: %d files", len(self.local_manifest))
            await self.send("manifest", {"files": self.local_manifest})
            logger.info("Waiting for remote manifest...")
            await self._manifest_event.wait()
            logger.info("Remote manifest received: %d files", len(self._remote_manifest))
            await self._request_needed()
            await self._sync_event.wait()
            self._initial_sync_done = True
            self._start_watcher()
            logger.info("Initial sync complete, real-time sync active")
            await self._recv_task
        except Exception as e:
            logger.error("Session error: %s", e)
        finally:
            self._cleanup()

    async def run_as_server(self):
        try:
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("Waiting for client manifest...")
            await self._manifest_event.wait()
            logger.info("Client manifest received: %d files", len(self._remote_manifest))
            self.local_manifest = scan_directory(self.root_path)
            logger.info("Local manifest: %d files", len(self.local_manifest))
            await self.send("manifest", {"files": self.local_manifest})
            await self._request_needed()
            await self._sync_event.wait()
            self._initial_sync_done = True
            self._start_watcher()
            logger.info("Initial sync complete, real-time sync active")
            await self._recv_task
        except Exception as e:
            logger.error("Session error: %s", e)
        finally:
            self._cleanup()

    async def _request_needed(self):
        needs = diff_manifests(self.local_manifest, self._remote_manifest)
        self._pending_transfers = {n["path"] for n in needs}
        logger.info("Need to pull %d files from remote", len(needs))
        for need in needs:
            indices = need["block_indices"]
            await self.send(
                "request_blocks",
                {"path": need["path"], "indices": indices if indices else "all"},
            )
        if not needs:
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
                    logger.error("Error handling %s: %s", msg_type, e)
        self._running = False

    async def _handle_manifest(self, data, _payload):
        self._remote_manifest = {
            k: v
            for k, v in data.get("files", {}).items()
            if not v.get("is_dir")
        }
        self._manifest_event.set()

    async def _handle_request_blocks(self, data, _payload):
        path = data["path"]
        indices = data.get("indices", "all")
        filepath = os.path.join(self.root_path, path)

        if not os.path.isfile(filepath):
            await self.send("transfer_done", {"path": path, "size": 0})
            return

        if indices == "all":
            blocks = compute_file_blocks(filepath)
            indices = [b["index"] for b in blocks]

        file_size = os.path.getsize(filepath)

        for idx in indices:
            block = read_block(self.root_path, path, idx)
            if block is not None:
                await self.send(
                    "block_data", {"path": path, "index": idx}, payload=block
                )

        await self.send("transfer_done", {"path": path, "size": file_size})

    async def _handle_block_data(self, data, payload):
        path = data["path"]
        index = data["index"]
        if path not in self._pending_blocks:
            self._pending_blocks[path] = []
        self._pending_blocks[path].append((index, payload))

    async def _handle_transfer_done(self, data, _payload):
        path = data["path"]
        size = data.get("size")
        if path in self._pending_blocks:
            self._synced_files[path] = time.time()
            write_blocks(
                self.root_path,
                path,
                self._pending_blocks.pop(path),
                expected_size=size,
            )
            logger.info("Synced: %s", path)
        self._pending_transfers.discard(path)
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
            logger.info("Remote delete: %s", path)
        elif event == "create" and is_dir:
            full = os.path.join(self.root_path, path)
            os.makedirs(full, exist_ok=True)
            logger.info("Remote mkdir: %s", path)
        elif event in ("create", "modify") and not is_dir:
            remote_mtime = data.get("mtime", 0)
            fpath = os.path.join(self.root_path, path)
            if os.path.exists(fpath) and not os.path.isdir(fpath):
                local_mtime = os.path.getmtime(fpath)
                if local_mtime > remote_mtime:
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

            if needed:
                self._pending_transfers.add(path)
                await self.send(
                    "request_blocks",
                    {"path": path, "indices": needed},
                )
            else:
                self._synced_files[path] = time.time()
                expected_size = data.get("size")
                if expected_size is not None and os.path.exists(fpath):
                    with open(fpath, "r+b") as f:
                        f.truncate(expected_size)
                    logger.info("File already in sync: %s", path)

    def _start_watcher(self):
        self._watcher = FileWatcher(
            self.root_path, self._on_file_change, self.loop
        )
        self._watcher.start()
        logger.info("File watcher started for: %s", self.root_path)

    async def _on_file_change(self, event_type, rel_path, is_dir):
        if rel_path in self._synced_files:
            if time.time() - self._synced_files[rel_path] < 2.0:
                return
            del self._synced_files[rel_path]

        if not self._running:
            return

        logger.info("Local change: %s %s", event_type, rel_path)

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
        if self._watcher:
            self._watcher.stop()
        if not self.writer.is_closing():
            try:
                self.writer.close()
            except Exception:
                pass
        logger.info("Session closed")
