# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_sync_engine.py

# Run a single test
pytest tests/test_sync_engine.py::TestComputeFileBlocks::test_multiple_blocks -v

# Start TCP server
python -m orcasync server --host 0.0.0.0 --port 8384

# Start TCP client
python -m orcasync client --local /path/to/local --remote /path/to/remote --host 127.0.0.1 --port 8384

# Local sync (no TCP)
python -m orcasync local-sync --src /path/a --dst /path/b

# Local sync with fast start (skip hashing on initial scan)
python -m orcasync local-sync --src /path/a --dst /path/b --fast-start

# Named instance (log goes to logs/orcasync-work.{pid}.log)
python -m orcasync --name work local-sync --src /path/a --dst /path/b
```

No linting or type-checking tools are configured. Python 3.10+ required.

## Architecture

orcasync is a bidirectional file sync tool using asyncio, block-level delta transfers (128KB SHA-256 chunks), and watchdog-based real-time watching. It has two transport modes: TCP (remote) and local (in-process, no networking).

### Module responsibilities

- **`sync_engine.py`** — all filesystem logic: `scan_directory()` builds manifests of files+dirs with block hashes; `diff_manifests()` computes what each side needs; `read_block()` / `write_blocks()` do the actual I/O. All internal paths use `/` regardless of OS.
- **`session.py` (`SyncSession`)** — TCP sync orchestration: exchanges manifests, requests blocks, handles incoming messages via `_recv_loop()`, activates `FileWatcher` after initial sync, and broadcasts `file_event` messages on changes.
- **`local_sync.py` (`LocalSyncSession`)** — same sync logic without TCP; calls `sync_engine` directly and runs two `FileWatcher` instances (one per directory).
- **`protocol.py`** — binary wire format: `[4-byte big-endian header length][JSON header][raw payload]`. Message types: `init`, `init_ack`, `manifest`, `request_blocks`, `block_data`, `transfer_done`, `sync_done`, `file_event`.
- **`watcher.py` (`FileWatcher`)** — wraps watchdog with 0.5s debounce and asyncio callback integration. Strips `\\?\` Windows long-path prefixes and normalizes separators to `/`.
- **`gitignore.py` (`GitIgnoreMatcher`)** — `.syncignore` at root takes precedence over recursive `.gitignore` files; always ignores `.git/`.
- **`manifest_db.py`** — SQLite-backed cache for file hashes. Stored in `<sync-root>/.orcasync/manifest.db` (or `--state-dir`). Avoids rehashing unchanged files across restarts using mtime+size as cache keys.
- **`rescanner.py`** — periodic background rescan that runs `scan_directory()` incrementally and triggers sync for any detected drift.
- **`server.py` / `client.py`** — TCP entry points; server creates one `SyncSession` per accepted connection.
- **`cli.py`** — argparse for three subcommands: `server`, `client`, `local-sync`. Global flags: `--name`, `--log-level`, `--log-format`, `--log-file`, `--log-backup-count`, `--rescan-interval-s`, `--state-dir`.
- **`logging_util.py`** — `setup_logging()` configures stderr + optional daily-rotating file handler. Filename placeholders: `{name}`, `{role}`, `{pid}`. PID is auto-injected if absent so each process writes its own file.

### TCP sync flow

1. Client sends `init` with the remote path; server replies `init_ack`
2. Both sides call `scan_directory()` and exchange `manifest` messages
3. Each side diffs manifests and sends `request_blocks` for what it needs
4. Blocks are transferred via `block_data` messages and written with `write_blocks()`
5. `transfer_done` / `sync_done` signals completion; file watchers activate
6. Real-time: a `file_event` carries updated block hashes so only changed blocks are re-fetched

### Echo avoidance

`session.py` tracks recently-synced files in a 2-second window to prevent feedback loops where applying a remote change triggers a local event that re-sends the same change back.

### Path normalization

`sync_engine.normalize_path()` converts `os.sep` to `/` for all manifest keys and protocol messages. `read_block()` and `write_blocks()` convert back to `os.sep` for actual file I/O. `watcher._Handler._rel()` strips the Windows `\\?\` prefix before normalizing.

### Logging

`setup_logging()` in `logging_util.py` is called once at startup from `cli.py`. It always adds a stderr handler and, when a log file path is provided (default: `logs/orcasync.log`), adds a `TimedRotatingFileHandler` (rotates at midnight, keeps 30 days). The filename has PID injected automatically so concurrent instances never share a file.
