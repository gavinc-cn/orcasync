import struct
import json
import asyncio


async def send_message(
    writer: asyncio.StreamWriter,
    msg_type: str,
    data: dict | None = None,
    payload: bytes = b"",
):
    header = {"type": msg_type, "payload_len": len(payload)}
    if data:
        header.update(data)
    header_bytes = json.dumps(header).encode("utf-8")
    writer.write(struct.pack("!I", len(header_bytes)) + header_bytes + payload)
    await writer.drain()


async def recv_message(reader: asyncio.StreamReader):
    raw = await reader.readexactly(4)
    header_len = struct.unpack("!I", raw)[0]
    header_bytes = await reader.readexactly(header_len)
    header = json.loads(header_bytes.decode("utf-8"))
    msg_type = header.pop("type")
    payload_len = header.pop("payload_len", 0)
    payload = await reader.readexactly(payload_len) if payload_len else b""
    return msg_type, header, payload
