import asyncio
import logging
import os

from .protocol import send_message, recv_message
from .session import SyncSession
from .logging_util import log_event

logger = logging.getLogger("orcasync.client")


async def run_client(local_path, remote_path, host, port, use_gitignore=True,
                     rescan_interval_s=600):
    local_path = os.path.abspath(local_path)
    os.makedirs(local_path, exist_ok=True)

    log_event(logger, logging.INFO, "client.connecting", host=host, port=port)
    reader, writer = await asyncio.open_connection(host, port)
    log_event(logger, logging.INFO, "client.connected", host=host, port=port)

    try:
        await send_message(writer, "init", {"remote_path": remote_path})

        msg_type, data, _ = await asyncio.wait_for(
            recv_message(reader), timeout=10
        )
        if msg_type != "init_ack" or data.get("status") != "ok":
            log_event(logger, logging.ERROR, "client.init_rejected", response=str(data))
            writer.close()
            return

        log_event(
            logger, logging.INFO, "client.init_ok",
            local=local_path, remote=remote_path,
        )

        session = SyncSession(
            local_path, reader, writer, asyncio.get_running_loop(),
            use_gitignore=use_gitignore,
            rescan_interval_s=rescan_interval_s,
        )
        await session.run_as_client()
    except ConnectionRefusedError:
        log_event(logger, logging.ERROR, "client.refused", host=host, port=port)
    except asyncio.TimeoutError:
        log_event(logger, logging.ERROR, "client.timeout")
    except Exception as e:
        log_event(logger, logging.ERROR, "client.error", error=str(e))
    finally:
        if not writer.is_closing():
            writer.close()
