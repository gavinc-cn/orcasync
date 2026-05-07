import argparse
import logging
import asyncio
import sys

from .server import run_server
from .client import run_client
from .local_sync import LocalSyncSession


def main():
    parser = argparse.ArgumentParser(
        prog="orcasync",
        description="orcasync - Bidirectional file synchronization tool",
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
        "--no-gitignore", action="store_true", help="Disable .gitignore filtering"
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
        "--no-gitignore", action="store_true", help="Disable .gitignore filtering"
    )

    local_parser = subparsers.add_parser("local-sync", help="Sync two local folders directly (no TCP)")
    local_parser.add_argument("--src", "-s", required=True, help="Source folder path")
    local_parser.add_argument("--dst", "-d", required=True, help="Destination folder path")
    local_parser.add_argument(
        "--no-gitignore", action="store_true", help="Disable .gitignore filtering"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    use_gitignore = not getattr(args, "no_gitignore", False)

    if args.command == "server":
        try:
            asyncio.run(run_server(args.host, args.port, use_gitignore=use_gitignore))
        except KeyboardInterrupt:
            print("\nServer stopped.")
    elif args.command == "client":
        try:
            asyncio.run(run_client(args.local, args.remote, args.host, args.port, use_gitignore=use_gitignore))
        except KeyboardInterrupt:
            print("\nClient stopped.")
    elif args.command == "local-sync":
        session = LocalSyncSession(args.src, args.dst, use_gitignore=use_gitignore)
        try:
            asyncio.run(session.run())
        except KeyboardInterrupt:
            session.stop()
            print("\nLocal sync stopped.")


if __name__ == "__main__":
    main()
