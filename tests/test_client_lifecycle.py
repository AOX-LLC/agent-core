"""Connection lifecycle against a real keep-alive HTTP server on loopback."""

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aox_agent_core import AgentClient, Mode, Tier
from support import FIXTURES, make_config

RESPONSE_BODY = (FIXTURES / "sdk" / "text_reply.json").read_bytes()


class _MessagesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep connections open, so the SDK pools them

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(RESPONSE_BODY)))
        self.end_headers()
        self.wfile.write(RESPONSE_BODY)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def local_api() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MessagesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def live_client(base_url: str, tmp_path: Path) -> AgentClient:
    config = make_config(
        tmp_path, mode=Mode.LIVE, anthropic={"base_url": base_url, "max_retries": 0}
    )
    return AgentClient(config, api_key="test-key-not-real")


def test_client_is_usable_again_after_close(local_api: str, tmp_path: Path) -> None:
    client = live_client(local_api, tmp_path)

    first = client.call_sync("one", tier=Tier.SMALL)
    client.close()
    second = client.call_sync("two", tier=Tier.SMALL)
    client.close()

    assert first.output == second.output


def test_sync_and_async_calls_can_share_a_client(local_api: str, tmp_path: Path) -> None:
    client = live_client(local_api, tmp_path)
    client.call_sync("from a script", tier=Tier.SMALL)

    async def call_from_another_loop() -> str:
        async with client:
            result = await client.call("from async code", tier=Tier.SMALL)
        return result.output

    assert asyncio.run(call_from_another_loop())
    client.call_sync("from a script again", tier=Tier.SMALL)
    client.close()


async def test_close_inside_a_running_loop_points_to_aclose(local_api: str, tmp_path: Path) -> None:
    client = live_client(local_api, tmp_path)
    await client.call("hello", tier=Tier.SMALL)
    await client.aclose()

    client.close()  # nothing to close: call_sync was never used
