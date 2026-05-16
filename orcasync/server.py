import asyncio
import logging
import os

from .protocol import send_message, recv_message
from .session import SyncSession
from .logging_util import log_event

logger = logging.getLogger("orcasync.server")


async def run_server(host="0.0.0.0", port=8384, use_gitignore=True,
                     rescan_interval_s=600):
    log_event(logger, logging.INFO, "server.listen", host=host, port=port)

    async def handle_client(reader, writer):
        addr = writer.get_extra_info("peername")
        log_event(logger, logging.INFO, "server.connection", peer=str(addr))
        try:
            msg_type, data, _ = await recv_message(reader)
            if msg_type != "init":
                log_event(
                    logger, logging.ERROR, "server.bad_init",
                    got=msg_type, peer=str(addr),
                )
                writer.close()
                return

            remote_path = os.path.abspath(data["remote_path"])
            os.makedirs(remote_path, exist_ok=True)
            await send_message(writer, "init_ack", {"status": "ok"})
            log_event(
                logger, logging.INFO, "server.init_ok",
                peer=str(addr), root=remote_path,
            )

            session = SyncSession(
                remote_path, reader, writer, asyncio.get_running_loop(),
                use_gitignore=use_gitignore,
                rescan_interval_s=rescan_interval_s,
            )
            await session.run_as_server()
        except Exception as e:
            log_event(logger, logging.ERROR, "server.handler_error", error=str(e))
        finally:
            if not writer.is_closing():
                writer.close()

    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()
