import asyncio
import logging
import os

from .protocol import send_message, recv_message
from .session import SyncSession

logger = logging.getLogger("orcasync")


async def run_server(host="0.0.0.0", port=8384, use_gitignore=True, ignore_file=None):
    logger.info("Server listening on %s:%d", host, port)

    async def handle_client(reader, writer):
        addr = writer.get_extra_info("peername")
        logger.info("Connection from %s", addr)
        try:
            msg_type, data, _ = await recv_message(reader)
            if msg_type != "init":
                logger.error("Expected init, got %s", msg_type)
                writer.close()
                return

            remote_path = os.path.abspath(data["remote_path"])
            os.makedirs(remote_path, exist_ok=True)
            await send_message(writer, "init_ack", {"status": "ok"})
            logger.info("Client syncing remote path: %s", remote_path)

            session = SyncSession(
                remote_path, reader, writer, asyncio.get_running_loop(),
                use_gitignore=use_gitignore, ignore_file=ignore_file,
            )
            await session.run_as_server()
        except Exception as e:
            logger.error("Client handler error: %s", e)
        finally:
            if not writer.is_closing():
                writer.close()

    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()
