import argparse
import asyncio
import os

from .server import run_server
from .client import run_client
from .local_sync import LocalSyncSession
from .logging_util import setup_logging


def main():
    parser = argparse.ArgumentParser(
        prog="orcasync",
        description="orcasync - Bidirectional file synchronization tool",
    )
    parser.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help=(
            "Instance name. Included in the log filename to distinguish "
            "multiple orcasync instances running on the same machine "
            "(e.g. --name work, --name backup). "
            "Also available as {name} in --log-file."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--log-format",
        default="text",
        choices=["text", "json"],
        help="Log format (default: text)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help=(
            "Log file path (rotates daily at midnight, keeps --log-backup-count days). "
            "Defaults to logs/orcasync.log (or logs/orcasync-NAME.log when --name is set) "
            "in the current directory. "
            "PID is injected before the extension automatically so each process writes "
            "to its own file (e.g. orcasync.log -> orcasync.12345.log). "
            "Supports {name}, {role}, and {pid} placeholders."
        ),
    )
    parser.add_argument(
        "--log-backup-count",
        type=int,
        default=30,
        metavar="N",
        help="Number of daily log backup files to keep (default: 30)",
    )
    parser.add_argument(
        "--rescan-interval-s",
        type=int,
        default=600,
        help="Periodic incremental rescan interval in seconds (default: 600; "
             "0 disables the rescanner)",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        metavar="DIR",
        help="External directory for orcasync state (staging files). "
             "Defaults to <sync-root>/.orcasync. Use this when the sync "
             "root is on a read-only or cross-device filesystem.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_parser = subparsers.add_parser("server", help="Start sync server")
    server_parser.add_argument(
        "--host", default="0.0.0.0", help="Listen address (default: 0.0.0.0)"
    )
    server_parser.add_argument(
        "--port", "-p", type=int, default=8384, help="Listen port (default: 8384)"
    )
    server_parser.add_argument(
        "--no-gitignore", action="store_true", help="Disable .gitignore/.syncignore filtering"
    )

    client_parser = subparsers.add_parser("client", help="Start sync client")
    client_parser.add_argument(
        "--local", "-l", required=True, help="Local folder path"
    )
    client_parser.add_argument(
        "--remote", "-r", required=True, help="Remote folder path on server"
    )
    client_parser.add_argument(
        "--host", "-H", default="127.0.0.1", help="Server address (default: 127.0.0.1)"
    )
    client_parser.add_argument(
        "--port", "-p", type=int, default=8384, help="Server port (default: 8384)"
    )
    client_parser.add_argument(
        "--no-gitignore", action="store_true", help="Disable .gitignore/.syncignore filtering"
    )

    local_parser = subparsers.add_parser("local-sync", help="Sync two local folders directly (no TCP)")
    local_parser.add_argument("--src", "-s", required=True, help="Source folder path")
    local_parser.add_argument("--dst", "-d", required=True, help="Destination folder path")
    local_parser.add_argument(
        "--no-gitignore", action="store_true", help="Disable .gitignore/.syncignore filtering"
    )
    local_parser.add_argument(
        "--fast-start", action="store_true", default=False,
        help="Skip hashing during initial scan; use mtime+size only. "
             "Starts syncing in seconds but hash verification is degraded "
             "until background rebuild completes. Risk: silent corruption "
             "undetectable if mtime matches but content differs.",
    )

    args = parser.parse_args()

    # Compute default log file path when --log-file is not specified.
    # With --name: logs/orcasync-NAME.log
    # Without --name: logs/orcasync.log
    log_file = args.log_file
    if log_file is None:
        name_part = f"-{args.name}" if args.name else ""
        log_file = os.path.join("logs", f"orcasync{name_part}.log")

    setup_logging(
        level=args.log_level,
        fmt=args.log_format,
        log_file=log_file,
        log_backup_count=args.log_backup_count,
        role=args.command,
        name=args.name,
    )

    use_gitignore = not getattr(args, "no_gitignore", False)

    rescan_s = args.rescan_interval_s
    state_dir = args.state_dir

    if args.command == "server":
        try:
            asyncio.run(run_server(
                args.host, args.port,
                use_gitignore=use_gitignore,
                rescan_interval_s=rescan_s,
                state_dir=state_dir,
            ))
        except KeyboardInterrupt:
            print("\nServer stopped.")
    elif args.command == "client":
        try:
            asyncio.run(run_client(
                args.local, args.remote, args.host, args.port,
                use_gitignore=use_gitignore,
                rescan_interval_s=rescan_s,
                state_dir=state_dir,
            ))
        except KeyboardInterrupt:
            print("\nClient stopped.")
    elif args.command == "local-sync":
        session = LocalSyncSession(
            args.src, args.dst,
            use_gitignore=use_gitignore,
            rescan_interval_s=rescan_s,
            state_dir=state_dir,
            fast_start=args.fast_start,
        )
        try:
            asyncio.run(session.run())
        except KeyboardInterrupt:
            session.stop()
            print("\nLocal sync stopped.")


if __name__ == "__main__":
    main()
