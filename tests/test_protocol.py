import asyncio
import struct
import json

import pytest

from orcasync.protocol import send_message, recv_message


@pytest.fixture
def pipe():
    async def make_pipe():
        return asyncio.open_connection(localhost=None)

    r = asyncio.Queue()
    return r


@pytest.mark.asyncio
async def test_message_no_payload():
    server_ready = asyncio.Event()

    async def server(reader, writer):
        msg_type, data, payload = await recv_message(reader)
        await send_message(writer, "echo", {"orig_type": msg_type, **data})
        if not writer.is_closing():
            writer.close()

    async def client(reader, writer):
        await send_message(writer, "hello", {"key": "value"})
        msg_type, data, payload = await recv_message(reader)
        assert msg_type == "echo"
        assert data["orig_type"] == "hello"
        assert data["key"] == "value"
        assert payload == b""
        if not writer.is_closing():
            writer.close()

    srv = await asyncio.start_server(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await client(reader, writer)
    srv.close()
    await srv.wait_closed()


@pytest.mark.asyncio
async def test_message_with_payload():
    async def server(reader, writer):
        msg_type, data, payload = await recv_message(reader)
        assert payload == b"\x00\x01\x02"
        await send_message(writer, "ack", {"got": msg_type}, payload=payload[::-1])
        if not writer.is_closing():
            writer.close()

    async def client(reader, writer):
        await send_message(writer, "data", {"n": 1}, payload=b"\x00\x01\x02")
        msg_type, data, payload = await recv_message(reader)
        assert msg_type == "ack"
        assert data["got"] == "data"
        assert payload == b"\x02\x01\x00"
        if not writer.is_closing():
            writer.close()

    srv = await asyncio.start_server(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await client(reader, writer)
    srv.close()
    await srv.wait_closed()


@pytest.mark.asyncio
async def test_message_with_none_data():
    async def server(reader, writer):
        msg_type, data, payload = await recv_message(reader)
        assert msg_type == "ping"
        assert "payload_len" in data
        if not writer.is_closing():
            writer.close()

    async def client(reader, writer):
        await send_message(writer, "ping")
        if not writer.is_closing():
            writer.close()

    srv = await asyncio.start_server(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await client(reader, writer)
    srv.close()
    await srv.wait_closed()


@pytest.mark.asyncio
async def test_large_payload():
    large = b"\xab" * (256 * 1024)

    async def server(reader, writer):
        msg_type, data, payload = await recv_message(reader)
        assert len(payload) == len(large)
        await send_message(writer, "ok", {"len": len(payload)})
        if not writer.is_closing():
            writer.close()

    async def client(reader, writer):
        await send_message(writer, "big", {}, payload=large)
        msg_type, data, payload = await recv_message(reader)
        assert msg_type == "ok"
        assert data["len"] == len(large)
        if not writer.is_closing():
            writer.close()

    srv = await asyncio.start_server(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await client(reader, writer)
    srv.close()
    await srv.wait_closed()


@pytest.mark.asyncio
async def test_multiple_messages_in_sequence():
    async def server(reader, writer):
        for expected in ["msg1", "msg2", "msg3"]:
            msg_type, data, payload = await recv_message(reader)
            await send_message(writer, f"{msg_type}_ack", {})
        if not writer.is_closing():
            writer.close()

    async def client(reader, writer):
        for name in ["msg1", "msg2", "msg3"]:
            await send_message(writer, name, {})
            msg_type, data, payload = await recv_message(reader)
            assert msg_type == f"{name}_ack"
        if not writer.is_closing():
            writer.close()

    srv = await asyncio.start_server(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await client(reader, writer)
    srv.close()
    await srv.wait_closed()
