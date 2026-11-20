import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SERVER = Path(__file__).parent / "server.py"
DOI = "10.48550/arxiv.1706.03762"  # "Attention Is All You Need"
STARTUP_TIMEOUT = 30.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_server():
    """Starts server.py over streamable-http and yields its MCP URL."""
    port = _free_port()
    env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, str(SERVER)], cwd=SERVER.parent, env=env)
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"Server exited with code {proc.returncode}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("Server did not start in time") from None
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def call_tool(url: str, arguments: dict) -> str:
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "get_article_by_doi" in {t.name for t in tools.tools}
            result = await session.call_tool("get_article_by_doi", arguments)
            assert not result.is_error
            return result.content[0].text


@pytest.fixture(scope="module")
def server_url():
    with running_server() as url:
        yield url


def test_get_article_by_doi(server_url):
    text = asyncio.run(call_tool(server_url, {"doi": DOI, "start_page": 1, "max_pages": 2}))
    assert text.startswith("[Pages 1-2 of ")
    assert "attention" in text.lower()


def test_start_page_beyond_end(server_url):
    text = asyncio.run(call_tool(server_url, {"doi": DOI, "start_page": 10_000}))
    assert "beyond the last page" in text


if __name__ == "__main__":
    with running_server() as url:
        test_get_article_by_doi(url)
        test_start_page_beyond_end(url)
        print("OK")
