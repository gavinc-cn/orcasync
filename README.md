# orcasync

A bidirectional file synchronization tool built with Python and asyncio.

orcasync keeps two directories in sync using block-level delta transfers. It watches for filesystem changes in real time and only sends the changed blocks of each file, minimizing bandwidth usage. Supports both TCP-based remote sync and direct local sync.

## Features

- **Bidirectional sync** — both sides can make changes and they propagate to the other
- **Block-level delta transfers** — uses 128KB block hashing (SHA-256) to transfer only changed portions
- **Real-time file watching** — uses [watchdog](https://github.com/gorakhargosh/watchdog) for immediate change detection
- **Empty folder sync** — directories (including empty ones) are synchronized between both sides
- **Local sync mode** — sync two local folders directly without TCP (single process)
- **Cross-platform** — works on Linux, macOS, and Windows with path normalization
- **Simple protocol** — lightweight JSON header + binary payload over TCP
- **SQLite manifest cache** — persists file hashes across restarts to avoid full rescans
- **Daily log rotation** — logs rotate at midnight with per-process files for safe concurrent use

## Installation

```bash
pip install -r requirements.txt
```

Requirements:
- Python 3.10+
- watchdog >= 3.0.0

## Usage

### Global options

These options apply to all subcommands:

```
--name NAME              Instance name, included in the log filename to
                         distinguish multiple concurrent instances
--log-level LEVEL        DEBUG / INFO / WARNING / ERROR (default: INFO)
--log-format FORMAT      text or json (default: text)
--log-file PATH          Log file path; defaults to logs/orcasync.log
                         (or logs/orcasync-NAME.log when --name is set).
                         Rotates daily. Supports {name}, {role}, {pid} placeholders.
--log-backup-count N     Days of log backups to keep (default: 30)
--rescan-interval-s N    Periodic rescan interval in seconds (default: 600; 0 disables)
--state-dir DIR          External directory for orcasync state files
```

### TCP Mode (Remote Sync)

Use this when syncing folders across different machines or network boundaries.

**Start a server:**

```bash
python -m orcasync server --host 0.0.0.0 --port 8384
```

**Start a client:**

```bash
python -m orcasync client --local /path/to/local/dir --remote /path/to/remote/dir --host <server-ip> --port 8384
```

The client connects to the server and syncs `--local` (client side) with `--remote` (server side). After initial sync, both sides watch for changes and propagate them in real time.

### Local Sync Mode

Use this when both folders are on the same machine. No TCP server needed.

```bash
python -m orcasync local-sync --src /path/to/source --dst /path/to/destination
```

Add `--fast-start` to skip hashing during the initial scan (uses mtime+size only). This starts syncing in seconds but delays full hash verification until a background rebuild completes.

### Running multiple instances

Use `--name` to keep log files separate when running several orcasync instances at the same time:

```bash
python -m orcasync --name work  local-sync --src D:\work-local  --dst D:\work-backup
python -m orcasync --name media local-sync --src D:\media-local --dst D:\media-backup
```

Logs go to `logs\orcasync-work.{pid}.log` and `logs\orcasync-media.{pid}.log` respectively.

## Architecture

```
orcasync/
├── cli.py           # CLI argument parsing (server/client/local-sync subcommands)
├── server.py        # TCP server — accepts connections, runs SyncSession as server
├── client.py        # TCP client — connects to server, runs SyncSession as client
├── session.py       # SyncSession — core TCP sync logic
├── local_sync.py    # LocalSyncSession — local sync without TCP
├── sync_engine.py   # File scanning, block hashing, diffing, reading/writing blocks
├── manifest_db.py   # SQLite-backed manifest cache (persists hashes across restarts)
├── rescanner.py     # Periodic background rescan
├── protocol.py      # Binary protocol: [4-byte header length][JSON header][payload bytes]
├── watcher.py       # FileWatcher — wraps watchdog with debounced async callbacks
├── gitignore.py     # .syncignore / .gitignore filtering
└── logging_util.py  # Structured logging setup (text/JSON, file rotation)
```

### Sync Flow (TCP Mode)

1. Client connects and sends `init` with the remote path
2. Both sides exchange file manifests (path, size, mtime, block hashes, directories)
3. Each side diffs manifests and requests only the blocks it needs
4. Blocks are transferred and written to disk; directories are created directly
5. File watchers activate for real-time sync

### Protocol

Messages use a simple binary format:

```
[4 bytes: header length (big-endian uint32)][JSON header][payload bytes]
```

The JSON header contains a `type` field and optional `payload_len`. Message types: `init`, `init_ack`, `manifest`, `request_blocks`, `block_data`, `transfer_done`, `sync_done`, `file_event`.

### Path Handling

All internal paths use forward slashes (`/`) for cross-platform consistency. The sync engine converts them to system-specific separators when reading/writing files. On Windows, long path prefixes (`\\?\`) are automatically stripped from watchdog events.

## License

[MIT](LICENSE)
