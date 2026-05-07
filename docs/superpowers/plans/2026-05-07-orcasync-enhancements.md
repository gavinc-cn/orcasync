# orcasync Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement empty folder sync, local sync mode (no TCP), and Windows path compatibility for orcasync.

**Architecture:** Extend `sync_engine` to handle directories in manifests, add a `LocalSyncSession` that reuses the same diff/transfer logic without TCP, and normalize all internal paths to `/` separators with Windows long-path prefix stripping.

**Tech Stack:** Python 3.10+, asyncio, watchdog

---

## File Structure

| File | Responsibility |
|------|---------------|
| `orcasync/sync_engine.py` | Manifest scanning (files + dirs), diffing, block I/O, path normalization |
| `orcasync/session.py` | TCP sync session — handle `mkdir` messages, path normalization |
| `orcasync/watcher.py` | File system watching — path normalization for Windows |
| `orcasync/local_sync.py` | Local sync session — no TCP, direct disk I/O |
| `orcasync/cli.py` | CLI — add `local-sync` subcommand and `--use-polling` |
| `tests/test_empty_folder.py` | Tests for empty folder sync |
| `tests/test_local_sync.py` | Tests for local sync mode |

---

## Task 1: Path Normalization (Foundation)

**Files:**
- Modify: `orcasync/sync_engine.py`
- Test: `tests/test_sync_engine.py`

**Why first:** All other tasks depend on consistent path handling.

- [ ] **Step 1: Write failing test for path normalization**

Add to `tests/test_sync_engine.py`:

```python
class TestPathNormalization:
    def test_scan_directory_uses_forward_slash(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        (sub / "file.txt").write_bytes(b"x")
        manifest = scan_directory(str(tmp_path))
        # Should use forward slash regardless of OS
        assert "a/b/file.txt" in manifest
        assert "a\\b\\file.txt" not in manifest
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /opt/win/other/orcasync
pytest tests/test_sync_engine.py::TestPathNormalization::test_scan_directory_uses_forward_slash -v
```

Expected: FAIL — `AssertionError` because paths use backslash on Windows.

- [ ] **Step 3: Implement path normalization in `scan_directory`**

Modify `orcasync/sync_engine.py`, change the `rel_path` computation:

```python
# In scan_directory, replace:
rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
# With:
rel_path = (os.path.join(rel_dir, fname) if rel_dir else fname).replace(os.sep, "/")
```

- [ ] **Step 4: Add `normalize_path` helper and use everywhere**

Add to `sync_engine.py`:

```python
def normalize_path(path):
    """Normalize path to use forward slashes."""
    return path.replace(os.sep, "/")
```

Also update `read_block`, `write_blocks`, `delete_path`, `ensure_parent_dir` to accept normalized paths and convert back for disk operations:

```python
def read_block(root_path, rel_path, block_index):
    filepath = os.path.join(root_path, rel_path.replace("/", os.sep))
    ...

def write_blocks(root_path, rel_path, blocks_data, expected_size=None):
    filepath = os.path.join(root_path, rel_path.replace("/", os.sep))
    ...

def delete_path(root_path, rel_path):
    full = os.path.join(root_path, rel_path.replace("/", os.sep))
    ...

def ensure_parent_dir(root_path, rel_path):
    parent = os.path.dirname(os.path.join(root_path, rel_path.replace("/", os.sep)))
    ...
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_sync_engine.py -v
```

Expected: All tests pass.

---

## Task 2: Empty Folder Sync — sync_engine

**Files:**
- Modify: `orcasync/sync_engine.py`
- Test: `tests/test_empty_folder.py`

- [ ] **Step 1: Write failing test for empty folder scanning**

Create `tests/test_empty_folder.py`:

```python
import os
import pytest
from orcasync.sync_engine import scan_directory, diff_manifests, ensure_dir


class TestEmptyFolderSync:
    def test_scan_directory_includes_empty_dirs(self, tmp_path):
        # Create nested empty dirs
        empty1 = tmp_path / "empty1"
        empty1.mkdir()
        nested = tmp_path / "parent" / "child"
        nested.mkdir(parents=True)
        # Add a file elsewhere so directory isn't totally empty
        (tmp_path / "file.txt").write_bytes(b"x")
        
        manifest = scan_directory(str(tmp_path))
        
        assert "empty1" in manifest
        assert manifest["empty1"]["is_dir"] is True
        assert "parent/child" in manifest
        assert manifest["parent/child"]["is_dir"] is True
        assert "file.txt" in manifest
        assert manifest["file.txt"]["is_dir"] is False
    
    def test_scan_directory_empty_root(self, tmp_path):
        manifest = scan_directory(str(tmp_path))
        assert manifest == {}
    
    def test_diff_manifests_dir_missing_locally(self, tmp_path):
        local = {}
        remote = {
            "empty_dir": {"path": "empty_dir", "is_dir": True, "mtime": 100.0}
        }
        needs = diff_manifests(local, remote)
        assert len(needs) == 1
        assert needs[0]["path"] == "empty_dir"
        assert needs[0].get("is_dir") is True
    
    def test_diff_manifests_dir_already_exists(self, tmp_path):
        local = {
            "empty_dir": {"path": "empty_dir", "is_dir": True, "mtime": 100.0}
        }
        remote = {
            "empty_dir": {"path": "empty_dir", "is_dir": True, "mtime": 100.0}
        }
        needs = diff_manifests(local, remote)
        assert len(needs) == 0
    
    def test_ensure_dir_creates_nested(self, tmp_path):
        ensure_dir(str(tmp_path), "a/b/c")
        assert (tmp_path / "a" / "b" / "c").is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_empty_folder.py -v
```

Expected: FAIL — empty directories not in manifest, `diff_manifests` doesn't handle dirs, `ensure_dir` doesn't exist.

- [ ] **Step 3: Implement directory scanning in `scan_directory`**

Modify `orcasync/sync_engine.py`:

```python
def scan_directory(root_path):
    root = os.path.abspath(root_path)
    os.makedirs(root, exist_ok=True)
    manifest = {}

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        
        # Add directories
        for dname in dirnames:
            dpath = os.path.join(dirpath, dname)
            rel_path = os.path.join(rel_dir, dname).replace(os.sep, "/") if rel_dir else dname
            try:
                stat = os.stat(dpath)
                manifest[rel_path] = {
                    "path": rel_path,
                    "is_dir": True,
                    "mtime": stat.st_mtime,
                }
            except (OSError, IOError):
                continue

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel_path = os.path.join(rel_dir, fname).replace(os.sep, "/") if rel_dir else fname
            try:
                stat = os.stat(fpath)
                blocks = compute_file_blocks(fpath)
                manifest[rel_path] = {
                    "path": rel_path,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "is_dir": False,
                    "blocks": blocks,
                }
            except (OSError, IOError):
                continue

    return manifest
```

- [ ] **Step 4: Implement directory diffing in `diff_manifests`**

Modify `diff_manifests`:

```python
def diff_manifests(local_manifest, remote_manifest):
    needs = []
    for path, remote_info in remote_manifest.items():
        if remote_info.get("is_dir"):
            local_info = local_manifest.get(path)
            if local_info is None or not local_info.get("is_dir"):
                needs.append({"path": path, "is_dir": True})
            continue
        
        local_info = local_manifest.get(path)
        if local_info is None or local_info.get("is_dir"):
            needs.append({"path": path, "block_indices": None})
            continue
        if _same_blocks(local_info.get("blocks", []), remote_info.get("blocks", [])):
            continue
        if remote_info.get("mtime", 0) <= local_info.get("mtime", 0):
            continue
        local_hashes = {b["index"]: b["hash"] for b in local_info.get("blocks", [])}
        changed = [
            b["index"]
            for b in remote_info.get("blocks", [])
            if b["hash"] != local_hashes.get(b["index"])
        ]
        if changed:
            needs.append({"path": path, "block_indices": changed})
    return needs
```

- [ ] **Step 5: Add `ensure_dir` helper**

Add to `sync_engine.py`:

```python
def ensure_dir(root_path, rel_path):
    full = os.path.join(root_path, rel_path.replace("/", os.sep))
    os.makedirs(full, exist_ok=True)
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_empty_folder.py -v
```

Expected: All tests pass.

---

## Task 3: Empty Folder Sync — session.py

**Files:**
- Modify: `orcasync/session.py`

- [ ] **Step 1: Modify `_request_needed` to handle directories**

```python
async def _request_needed(self):
    needs = diff_manifests(self.local_manifest, self._remote_manifest)
    file_needs = [n for n in needs if not n.get("is_dir")]
    dir_needs = [n for n in needs if n.get("is_dir")]
    
    self._pending_transfers = {n["path"] for n in file_needs}
    logger.info("Need to pull %d files and %d dirs from remote", len(file_needs), len(dir_needs))
    
    # Create directories immediately (no need to request blocks)
    for need in dir_needs:
        ensure_dir(self.root_path, need["path"])
        logger.info("Created dir: %s", need["path"])
    
    for need in file_needs:
        indices = need["block_indices"]
        await self.send(
            "request_blocks",
            {"path": need["path"], "indices": indices if indices else "all"},
        )
    if not file_needs and not dir_needs:
        await self.send("sync_done", {})
        self._check_sync_complete()
```

- [ ] **Step 2: Update `_handle_manifest` to include directories**

No change needed — it already stores all entries from `files` key, and directories will be included.

- [ ] **Step 3: Update `_handle_file_event` for directory creation**

No change needed — it already handles `create` + `is_dir` in real-time sync.

- [ ] **Step 4: Run integration test for empty folder sync**

```bash
PY=/tmp/orcasync_env/bin/python3
SRC=/tmp/orca_empty_test/src
DST=/tmp/orca_empty_test/dst
rm -rf "$SRC" "$DST"
mkdir -p "$SRC/empty_dir" "$SRC/parent/nested_empty"
echo "file" > "$SRC/file.txt"

$PY -m orcasync server --host 127.0.0.1 --port 18387 2>/tmp/orca_empty_server.log &
SERVER_PID=$!
sleep 1
$PY -m orcasync client --local "$SRC" --remote "$DST" --host 127.0.0.1 --port 18387 2>/tmp/orca_empty_client.log &
CLIENT_PID=$!
sleep 3

echo "=== DST contents ==="
find "$DST" -type d | sort
find "$DST" -type f | sort

kill $SERVER_PID $CLIENT_PID 2>/dev/null
wait $SERVER_PID $CLIENT_PID 2>/dev/null
```

Expected: `empty_dir` and `parent/nested_empty` exist in DST.

---

## Task 4: Windows Path Compatibility — watcher.py

**Files:**
- Modify: `orcasync/watcher.py`

- [ ] **Step 1: Normalize paths in `_Handler._rel`**

```python
def _rel(self, path):
    # Strip Windows long path prefix
    if path.startswith("\\\\?\\"):
        path = path[4:]
    rel = os.path.relpath(path, self.root_path)
    return rel.replace(os.sep, "/")
```

- [ ] **Step 2: Test with a simple script**

No new tests needed — covered by existing integration tests.

---

## Task 5: Local Sync Mode

**Files:**
- Create: `orcasync/local_sync.py`
- Modify: `orcasync/cli.py`
- Test: `tests/test_local_sync.py`

- [ ] **Step 1: Write failing test for local sync**

Create `tests/test_local_sync.py`:

```python
import os
import tempfile
import pytest
from orcasync.local_sync import LocalSyncSession


class TestLocalSyncSession:
    def test_initial_sync_bidirectional(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            # Setup initial files
            with open(os.path.join(src, "src_only.txt"), "w") as f:
                f.write("from src")
            with open(os.path.join(dst, "dst_only.txt"), "w") as f:
                f.write("from dst")
            
            import asyncio
            async def run():
                session = LocalSyncSession(src, dst)
                await session.run_initial_sync()
                
                assert os.path.exists(os.path.join(dst, "src_only.txt"))
                assert os.path.exists(os.path.join(src, "dst_only.txt"))
                with open(os.path.join(dst, "src_only.txt")) as f:
                    assert f.read() == "from src"
                with open(os.path.join(src, "dst_only.txt")) as f:
                    assert f.read() == "from dst"
            
            asyncio.run(run())
    
    def test_initial_sync_empty_folders(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            os.makedirs(os.path.join(src, "empty_dir"))
            
            import asyncio
            async def run():
                session = LocalSyncSession(src, dst)
                await session.run_initial_sync()
                assert os.path.isdir(os.path.join(dst, "empty_dir"))
            
            asyncio.run(run())
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_local_sync.py -v
```

Expected: FAIL — `LocalSyncSession` doesn't exist.

- [ ] **Step 3: Implement `LocalSyncSession`**

Create `orcasync/local_sync.py`:

```python
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

logger = logging.getLogger("orcasync")


class LocalSyncSession:
    def __init__(self, src_path, dst_path):
        self.src_path = os.path.abspath(src_path)
        self.dst_path = os.path.abspath(dst_path)
        self._running = True
        self._src_watcher = None
        self._dst_watcher = None
        self._synced_files = {}
        self._lock = asyncio.Lock()
    
    async def run(self):
        await self.run_initial_sync()
        self._start_watchers()
        logger.info("Local sync active: %s <-> %s", self.src_path, self.dst_path)
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def run_initial_sync(self):
        src_manifest = scan_directory(self.src_path)
        dst_manifest = scan_directory(self.dst_path)
        
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
            self.src_path, self._on_src_change, loop
        )
        self._dst_watcher = FileWatcher(
            self.dst_path, self._on_dst_change, loop
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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_local_sync.py -v
```

Expected: Tests pass.

---

## Task 6: CLI — Add `local-sync` subcommand

**Files:**
- Modify: `orcasync/cli.py`

- [ ] **Step 1: Add `local-sync` subparser**

```python
from .local_sync import LocalSyncSession

# Add after client_parser:
local_parser = subparsers.add_parser("local-sync", help="Sync two local folders directly")
local_parser.add_argument("--src", "-s", required=True, help="Source folder path")
local_parser.add_argument("--dst", "-d", required=True, help="Destination folder path")
```

- [ ] **Step 2: Add handler for `local-sync`**

```python
elif args.command == "local-sync":
    try:
        session = LocalSyncSession(args.src, args.dst)
        asyncio.run(session.run())
    except KeyboardInterrupt:
        session.stop()
        print("\nLocal sync stopped.")
```

- [ ] **Step 3: Test CLI**

```bash
python -m orcasync local-sync --src /tmp/test_src --dst /tmp/test_dst
```

Expected: Should start syncing two local folders.

---

## Task 7: Final Integration Test

**Files:**
- All

- [ ] **Step 1: Run all existing tests**

```bash
pytest tests/ -v
```

Expected: All tests pass.

- [ ] **Step 2: End-to-end TCP test with empty folders**

```bash
PY=/tmp/orcasync_env/bin/python3
SRC=/tmp/orca_final/src
DST=/tmp/orca_final/dst
rm -rf "$SRC" "$DST"
mkdir -p "$SRC/empty1" "$SRC/parent/nested"
echo "hello" > "$SRC/file.txt"
echo "dst file" > "$DST/dst_file.txt"

$PY -m orcasync server --host 127.0.0.1 --port 18388 2>/tmp/orca_final_server.log &
SERVER_PID=$!
sleep 1
$PY -m orcasync client --local "$SRC" --remote "$DST" --host 127.0.0.1 --port 18388 2>/tmp/orca_final_client.log &
CLIENT_PID=$!
sleep 3

echo "=== SRC ==="
find "$SRC" | sort
echo "=== DST ==="
find "$DST" | sort

kill $SERVER_PID $CLIENT_PID 2>/dev/null
wait $SERVER_PID $CLIENT_PID 2>/dev/null
```

Expected: Both sides have `empty1`, `parent/nested`, `file.txt`, `dst_file.txt`.

- [ ] **Step 3: End-to-end local sync test**

```bash
PY=/tmp/orcasync_env/bin/python3
SRC=/tmp/orca_local/src
DST=/tmp/orca_local/dst
rm -rf "$SRC" "$DST"
mkdir -p "$SRC/empty" "$SRC/nested/dir"
echo "src content" > "$SRC/src_file.txt"
echo "dst content" > "$DST/dst_file.txt"

# Run local sync for 3 seconds
timeout 3 $PY -m orcasync local-sync --src "$SRC" --dst "$DST" 2>/tmp/orca_local.log || true

echo "=== SRC ==="
find "$SRC" | sort
echo "=== DST ==="
find "$DST" | sort
```

Expected: Both sides have all files and directories.

---

## Spec Coverage Check

| Spec Requirement | Task |
|------------------|------|
| 空文件夹初始同步 | Task 2, Task 3 |
| 空文件夹实时同步 | Task 3 (session.py already handles dir create events) |
| 本地模式双向同步 | Task 5, Task 6 |
| Windows 路径标准化 | Task 1, Task 4 |
| Windows 长路径前缀 | Task 4 |

---

## Placeholder Scan

No placeholders found. All steps have concrete code and commands.

## Type Consistency Check

- `scan_directory` returns manifest with `"is_dir": bool`
- `diff_manifests` returns needs with `"is_dir": True` for directories
- `LocalSyncSession._pull_file` checks `need.get("is_dir")`
- Path normalization uses `"/"` consistently

All consistent.
