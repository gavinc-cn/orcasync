# Documentation, License, and Unit Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add README.md documentation, MIT LICENSE, and comprehensive unit tests for orcasync.

**Architecture:** README covers project overview, installation, usage, and architecture. LICENSE is standard MIT. Tests cover sync_engine (pure functions, file I/O), protocol (binary serialization), and cli (argument parsing) using pytest.

**Tech Stack:** Python 3.10+, pytest, asyncio

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `README.md` | Project documentation |
| Create | `LICENSE` | MIT License |
| Create | `tests/__init__.py` | Test package marker |
| Create | `tests/test_sync_engine.py` | Tests for sync_engine module |
| Create | `tests/test_protocol.py` | Tests for protocol module |
| Create | `tests/test_cli.py` | Tests for cli module |

---

### Task 1: Add MIT LICENSE

**Files:**
- Create: `LICENSE`

- [ ] **Step 1: Create LICENSE file**

```
MIT License

Copyright (c) 2026 orcasync contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 2: Commit**

```bash
git add LICENSE
git commit -m "docs: add MIT license"
```

---

### Task 2: Add README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create README.md**

Content covers: project description, features, installation, quick start (server + client), architecture overview, license reference.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with usage and architecture"
```

---

### Task 3: Unit tests for sync_engine

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_sync_engine.py`
- Test: `tests/test_sync_engine.py`

Tests for: `compute_file_blocks`, `scan_directory`, `diff_manifests`, `read_block`, `write_blocks`, `delete_path`, `_same_blocks`, `ensure_parent_dir`.

- [ ] **Step 1: Write tests for compute_file_blocks**

Test with a file smaller than BLOCK_SIZE, a file exactly BLOCK_SIZE, a file spanning multiple blocks, and an empty file. Verify index, size, and sha256 hash correctness.

- [ ] **Step 2: Write tests for scan_directory**

Create a temp directory with nested files and subdirs. Verify manifest contains correct relative paths, sizes, mtimes, and block info for each file.

- [ ] **Step 3: Write tests for diff_manifests**

Cover cases: remote file missing locally (full pull needed), identical blocks (no pull), newer remote with changed blocks (partial pull), older remote (skip).

- [ ] **Step 4: Write tests for read_block and write_blocks**

Write known data, read back specific blocks, verify roundtrip integrity.

- [ ] **Step 5: Write tests for delete_path and ensure_parent_dir**

Test deleting files and directories, creating parent directories.

- [ ] **Step 6: Run all tests and verify pass**

```bash
python -m pytest tests/test_sync_engine.py -v
```

- [ ] **Step 7: Commit**

```bash
git add tests/__init__.py tests/test_sync_engine.py
git commit -m "test: add unit tests for sync_engine"
```

---

### Task 4: Unit tests for protocol

**Files:**
- Create: `tests/test_protocol.py`
- Test: `tests/test_protocol.py`

Tests for: `send_message`, `recv_message` roundtrip via asyncio piped streams.

- [ ] **Step 1: Write protocol roundtrip tests**

Test: message with no payload, message with payload, message with data dict. Use `asyncio.open_connection` with a local socket or pipe to create reader/writer pair.

- [ ] **Step 2: Run tests and verify pass**

```bash
python -m pytest tests/test_protocol.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_protocol.py
git commit -m "test: add unit tests for protocol"
```

---

### Task 5: Unit tests for cli

**Files:**
- Create: `tests/test_protocol.py` (already exists)
- Create: `tests/test_cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write CLI argument parsing tests**

Test server subcommand with default host/port, client subcommand with required args, missing required args raises error.

- [ ] **Step 2: Run tests and verify pass**

```bash
python -m pytest tests/test_cli.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_cli.py
git commit -m "test: add unit tests for cli argument parsing"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v
```

- [ ] **Step 2: Verify all files committed**

```bash
git status
```
