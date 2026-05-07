# GitIgnore Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `.gitignore` support to orcasync file synchronization, behaving exactly like Git: recursive `.gitignore` files, standard ignore patterns, default `.git/` exclusion, with `--no-gitignore` CLI flag to disable.

**Architecture:** Introduce `GitIgnoreMatcher` class using `pathspec` library. Integrate into both initial scan (`sync_engine.py`) and real-time watching (`watcher.py`). Each sync side applies its own `.gitignore` independently. `.gitignore` files themselves are never ignored so they sync naturally.

**Tech Stack:** Python 3.10+, pathspec>=0.12.0, existing orcasync codebase (watchdog, asyncio)

---

## File Map

| File | Responsibility |
|------|--------------|
| `orcasync/gitignore.py` | New. `GitIgnoreMatcher` class: reads all `.gitignore` files recursively, builds `pathspec.GitIgnoreSpec`, provides `is_ignored()` |
| `orcasync/sync_engine.py` | Modified. `scan_directory` accepts optional `gitignore_matcher`, filters ignored files/dirs during walk |
| `orcasync/watcher.py` | Modified. `FileWatcher` accepts optional `gitignore_matcher`, drops events for ignored paths |
| `orcasync/cli.py` | Modified. Add `--no-gitignore` flag to `server`, `client`, `local-sync` subcommands |
| `orcasync/session.py` | Modified. Create `GitIgnoreMatcher` for server/client, pass to `scan_directory` and `FileWatcher` |
| `orcasync/local_sync.py` | Modified. Create `GitIgnoreMatcher` for both src/dst, pass to `scan_directory` and `FileWatcher` |
| `requirements.txt` | Modified. Add `pathspec>=0.12.0` |
| `tests/test_gitignore.py` | New. Unit tests for `GitIgnoreMatcher` and integration tests for scan + watcher filtering |

---

## Task 1: Add pathspec to requirements

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add pathspec dependency**

```
watchdog>=3.0.0
pathspec>=0.12.0
```

- [ ] **Step 2: Install dependency**

Run: `pip install pathspec>=0.12.0`
Expected: Installs successfully

---

## Task 2: Implement GitIgnoreMatcher core class

**Files:**
- Create: `orcasync/gitignore.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gitignore.py`:

```python
import os
import tempfile
import pytest
from orcasync.gitignore import GitIgnoreMatcher


class TestGitIgnoreMatcher:
    def test_basic_ignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("foo.pyc", is_dir=False) is True
            assert matcher.is_ignored("foo.py", is_dir=False) is False

    def test_dir_suffix(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("build/\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("build", is_dir=True) is True
            assert matcher.is_ignored("build", is_dir=False) is False

    def test_nested_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.log\n")
            os.makedirs(os.path.join(root, "sub"))
            with open(os.path.join(root, "sub", ".gitignore"), "w") as f:
                f.write("!important.log\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored("foo.log", is_dir=False) is True
            assert matcher.is_ignored("sub/important.log", is_dir=False) is False
            assert matcher.is_ignored("sub/other.log", is_dir=False) is True

    def test_default_git_ignore(self):
        with tempfile.TemporaryDirectory() as root:
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored(".git", is_dir=True) is True
            assert matcher.is_ignored(".git/config", is_dir=False) is True

    def test_gitignore_file_not_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write(".gitignore\n")
            matcher = GitIgnoreMatcher(root)
            assert matcher.is_ignored(".gitignore", is_dir=False) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitignore.py -v`
Expected: ImportError / ModuleNotFoundError for `orcasync.gitignore`

- [ ] **Step 3: Implement GitIgnoreMatcher**

Create `orcasync/gitignore.py`:

```python
import os
import pathspec


class GitIgnoreMatcher:
    """
    Reads all .gitignore files recursively under root_path and provides
    is_ignored() matching exactly like Git.
    """

    def __init__(self, root_path):
        self.root_path = os.path.abspath(root_path)
        self._specs = {}  # dir_rel_path -> GitIgnoreSpec
        self._load()

    def _load(self):
        """Walk the directory tree and load all .gitignore files."""
        # Always ignore .git directory
        base_spec = pathspec.GitIgnoreSpec.from_lines("gitwildmatch", [".git/"])
        self._specs[""] = base_spec

        for dirpath, dirnames, filenames in os.walk(self.root_path):
            rel_dir = os.path.relpath(dirpath, self.root_path)
            if rel_dir == ".":
                rel_dir = ""

            if ".gitignore" in filenames:
                gitignore_path = os.path.join(dirpath, ".gitignore")
                try:
                    with open(gitignore_path, "r", encoding="utf-8") as f:
                        lines = [line.rstrip("\n\r") for line in f]
                except (OSError, IOError):
                    lines = []

                # Build spec for this directory: parent spec + local rules
                parent_spec = self._specs.get(rel_dir)
                local_spec = pathspec.GitIgnoreSpec.from_lines("gitwildmatch", lines)
                if parent_spec is not None:
                    # Combine: parent rules first, then local rules (higher priority)
                    combined = pathspec.GitIgnoreSpec(parent_spec.patterns + local_spec.patterns)
                else:
                    combined = local_spec
                self._specs[rel_dir] = combined

            # Propagate spec to subdirectories that don't have their own .gitignore
            for dname in dirnames:
                child_rel = os.path.join(rel_dir, dname) if rel_dir else dname
                if child_rel not in self._specs:
                    parent_spec = self._specs.get(rel_dir)
                    if parent_spec is not None:
                        self._specs[child_rel] = parent_spec

    def is_ignored(self, rel_path, is_dir=False):
        """
        Check if a path (relative to root) is ignored.
        .gitignore files themselves are never ignored.
        """
        # Never ignore .gitignore files themselves
        basename = os.path.basename(rel_path)
        if basename == ".gitignore":
            return False

        # Find the directory containing this path
        dir_rel = os.path.dirname(rel_path)
        if dir_rel == ".":
            dir_rel = ""

        spec = self._specs.get(dir_rel)
        if spec is None:
            return False

        return spec.match_file(rel_path, is_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gitignore.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add orcasync/gitignore.py tests/test_gitignore.py requirements.txt
git commit -m "feat: add GitIgnoreMatcher using pathspec for recursive .gitignore support"
```

---

## Task 3: Integrate gitignore into sync_engine scan_directory

**Files:**
- Modify: `orcasync/sync_engine.py`
- Test: `tests/test_gitignore.py`

- [ ] **Step 1: Write failing integration test for scan_directory**

Add to `tests/test_gitignore.py`:

```python
from orcasync.sync_engine import scan_directory


class TestScanDirectoryWithGitignore:
    def test_scan_ignores_files(self):
        with tempfile.TemporaryDirectory() as root:
            # Create files
            with open(os.path.join(root, "keep.py"), "w") as f:
                f.write("pass")
            with open(os.path.join(root, "ignore.pyc"), "w") as f:
                f.write("bytecode")
            # Create .gitignore
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("*.pyc\n")
            # Create matcher
            matcher = GitIgnoreMatcher(root)
            manifest = scan_directory(root, gitignore_matcher=matcher)
            assert "keep.py" in manifest
            assert "ignore.pyc" not in manifest

    def test_scan_ignores_directories(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "build", "sub"))
            with open(os.path.join(root, "build", "sub", "file.txt"), "w") as f:
                f.write("content")
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("build/\n")
            matcher = GitIgnoreMatcher(root)
            manifest = scan_directory(root, gitignore_matcher=matcher)
            assert "build" not in manifest
            assert "build/sub" not in manifest
            assert "build/sub/file.txt" not in manifest

    def test_scan_without_gitignore(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "file.txt"), "w") as f:
                f.write("content")
            manifest = scan_directory(root, gitignore_matcher=None)
            assert "file.txt" in manifest
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitignore.py::TestScanDirectoryWithGitignore -v`
Expected: TypeError - scan_directory got unexpected keyword argument `gitignore_matcher`

- [ ] **Step 3: Modify scan_directory to accept and use gitignore_matcher**

Modify `orcasync/sync_engine.py`:

Change function signature on line 34:
```python
def scan_directory(root_path, gitignore_matcher=None):
```

In the `os.walk` loop, after computing `rel_path` for directories:
```python
        for dname in dirnames:
            dpath = os.path.join(dirpath, dname)
            rel_path = normalize_path(os.path.join(rel_dir, dname)) if rel_dir else dname
            # Skip ignored directories
            if gitignore_matcher is not None and gitignore_matcher.is_ignored(rel_path, is_dir=True):
                continue
            try:
                stat = os.stat(dpath)
                manifest[rel_path] = {
                    "path": rel_path,
                    "is_dir": True,
                    "mtime": stat.st_mtime,
                }
            except (OSError, IOError):
                continue
```

For files:
```python
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            rel_path = normalize_path(os.path.join(rel_dir, fname)) if rel_dir else fname
            # Skip ignored files
            if gitignore_matcher is not None and gitignore_matcher.is_ignored(rel_path, is_dir=False):
                continue
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gitignore.py::TestScanDirectoryWithGitignore -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add orcasync/sync_engine.py tests/test_gitignore.py
git commit -m "feat: integrate gitignore filtering into scan_directory"
```

---

## Task 4: Integrate gitignore into FileWatcher

**Files:**
- Modify: `orcasync/watcher.py`
- Test: `tests/test_gitignore.py`

- [ ] **Step 1: Write failing integration test for FileWatcher**

Add to `tests/test_gitignore.py`:

```python
import asyncio
import time
from orcasync.watcher import FileWatcher


class TestWatcherWithGitignore:
    def test_watcher_drops_ignored_events(self):
        loop = asyncio.new_event_loop()
        try:
            with tempfile.TemporaryDirectory() as root:
                with open(os.path.join(root, ".gitignore"), "w") as f:
                    f.write("*.tmp\n")
                matcher = GitIgnoreMatcher(root)
                events = []

                async def callback(event_type, rel_path, is_dir):
                    events.append((event_type, rel_path))

                watcher = FileWatcher(root, callback, loop, gitignore_matcher=matcher)
                watcher.start()
                try:
                    # Create a file that should be ignored
                    with open(os.path.join(root, "test.tmp"), "w") as f:
                        f.write("ignored")
                    # Create a file that should NOT be ignored
                    with open(os.path.join(root, "test.txt"), "w") as f:
                        f.write("kept")
                    time.sleep(1.0)
                    loop.run_until_complete(asyncio.sleep(0.1))
                finally:
                    watcher.stop()

                # The .txt file should generate an event, .tmp should not
                txt_events = [e for e in events if e[1] == "test.txt"]
                tmp_events = [e for e in events if e[1] == "test.tmp"]
                assert len(txt_events) > 0
                assert len(tmp_events) == 0
        finally:
            loop.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gitignore.py::TestWatcherWithGitignore -v`
Expected: TypeError - FileWatcher got unexpected keyword argument `gitignore_matcher`

- [ ] **Step 3: Modify FileWatcher to accept and use gitignore_matcher**

Modify `orcasync/watcher.py`:

Change `__init__`:
```python
class FileWatcher:
    def __init__(self, root_path, callback, loop, gitignore_matcher=None):
        self.root_path = os.path.abspath(root_path)
        self.callback = callback
        self.loop = loop
        self.gitignore_matcher = gitignore_matcher
        self.observer = Observer()
        self._pending = {}
        self._debounce = 0.5
```

In `_on_event`, add filtering at the start:
```python
    def _on_event(self, event_type, rel_path, is_dir):
        # Filter ignored paths
        if self.gitignore_matcher is not None and self.gitignore_matcher.is_ignored(rel_path, is_dir):
            return
        key = (event_type, rel_path)
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gitignore.py::TestWatcherWithGitignore -v`
Expected: Test PASS

- [ ] **Step 5: Commit**

```bash
git add orcasync/watcher.py tests/test_gitignore.py
git commit -m "feat: integrate gitignore filtering into FileWatcher"
```

---

## Task 5: Add --no-gitignore CLI flag

**Files:**
- Modify: `orcasync/cli.py`

- [ ] **Step 1: Read current cli.py to understand structure**

Read: `orcasync/cli.py`

- [ ] **Step 2: Add --no-gitignore to all subcommands**

Modify `orcasync/cli.py` to add `--no-gitignore` argument to `server`, `client`, and `local-sync` subcommands. Pass the flag value through to the respective functions/classes.

- [ ] **Step 3: Commit**

```bash
git add orcasync/cli.py
git commit -m "feat: add --no-gitignore CLI flag to all subcommands"
```

---

## Task 6: Wire gitignore into TCP session (session.py)

**Files:**
- Modify: `orcasync/session.py`

- [ ] **Step 1: Read current session.py**

Read: `orcasync/session.py`

- [ ] **Step 2: Modify SyncSession to create and pass GitIgnoreMatcher**

In `SyncSession.__init__`, accept `use_gitignore=True` parameter.
Create `GitIgnoreMatcher(self.root_path)` if enabled.
Pass it to `scan_directory()` calls and `FileWatcher` initialization.

- [ ] **Step 3: Modify server.py and client.py to pass the flag**

Modify `orcasync/server.py` and `orcasync/client.py` to read the CLI flag and pass `use_gitignore=not args.no_gitignore` to `SyncSession`.

- [ ] **Step 4: Commit**

```bash
git add orcasync/session.py orcasync/server.py orcasync/client.py
git commit -m "feat: wire gitignore matcher into TCP sync session"
```

---

## Task 7: Wire gitignore into local sync (local_sync.py)

**Files:**
- Modify: `orcasync/local_sync.py`

- [ ] **Step 1: Read current local_sync.py**

Read: `orcasync/local_sync.py`

- [ ] **Step 2: Modify LocalSyncSession to create and pass GitIgnoreMatcher for both sides**

In `LocalSyncSession.__init__`, accept `use_gitignore=True` parameter.
Create two `GitIgnoreMatcher` instances (one for src, one for dst) if enabled.
Pass them to `scan_directory()` calls and `FileWatcher` initializations.

- [ ] **Step 3: Commit**

```bash
git add orcasync/local_sync.py
git commit -m "feat: wire gitignore matcher into local sync session"
```

---

## Task 8: Run full test suite

**Files:**
- All modified files

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v`
Expected: All tests PASS (including new gitignore tests and existing tests)

- [ ] **Step 2: Commit any remaining changes**

```bash
git add .
git commit -m "test: verify gitignore support with full test suite"
```

---

## Spec Coverage Check

| Spec Requirement | Implementing Task |
|------------------|-------------------|
| 递归读取 `.gitignore` | Task 2 (`GitIgnoreMatcher._load`) |
| 规则行为一致 (pathspec gitwildmatch) | Task 2 |
| 双向同步时各自应用 | Task 6, 7 (两端各自创建 matcher) |
| 默认忽略 `.git` 目录 | Task 2 (base spec) |
| `--no-gitignore` CLI 开关 | Task 5 |
| 覆盖初始扫描和实时监听 | Task 3 (scan), Task 4 (watcher) |
| `.gitignore` 文件本身不过滤 | Task 2 (`is_ignored` early return) |
| 从 dirnames 删除被忽略目录 | Task 3 (continue instead of adding to manifest) |
