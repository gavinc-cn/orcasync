import asyncio
import logging
import os

from .protocol import send_message, recv_message
from .session import SyncSession

logger = logging.getLogger("orcasync")


async def run_client(local_path, remote_path, host, port):
    local_path = os.path.abspath(local_path)
    os.makedirs(local_path, exist_ok=True)

    logger.info("Connecting to %s:%d ...", host, port)
    reader, writer = await asyncio.open_connection(host, port)
    logger.info("Connected to %s:%d", host, port)

    try:
        await send_message(writer, "init", {"remote_path": remote_path})

        msg_type, data, _ = await asyncio.wait_for(
            recv_message(reader), timeout=10
        )
        if msg_type != "init_ack" or data.get("status") != "ok":
            logger.error("Server rejected init: %s", data)
            writer.close()
            return

        logger.info("Server accepted, syncing local=%s remote=%s", local_path, remote_path)

        session = SyncSession(
            local_path, reader, writer, asyncio.get_running_loop()
        )
        await session.run_as_client()
    except ConnectionRefusedError:
        logger.error("Connection refused: %s:%d", host, port)
    except asyncio.TimeoutError:
        logger.error("Timeout waiting for server response")
    except Exception as e:
        logger.error("Client error: %s", e)
    finally:
        if not writer.is_closing():
            writer.close()
