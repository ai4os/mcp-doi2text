import asyncio
import io
import logging
import os
import re
import sys
from collections import OrderedDict
from urllib.parse import quote

import anydoc
from dotenv import load_dotenv
import httpx
from mcp.server.mcpserver import MCPServer
from pypdf import PdfReader, PdfWriter


load_dotenv()

# stdout carries the MCP stdio protocol, so logs must go to stderr.
log = logging.getLogger("doi2text")
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s [doi2text] %(message)s", "%H:%M:%S"))
log.addHandler(_handler)
log.setLevel(logging.INFO)
log.propagate = False

CACHE_MAX_BYTES = 100 * 1024 * 1024
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "")
HTTP_TIMEOUT = 60.0

mcp = MCPServer("doi2text")


class TextCache:
    """LRU cache of per-page text, bounded by total UTF-8 size."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.size = 0
        self._items: OrderedDict[str, tuple[list[str], int]] = OrderedDict()

    def get(self, key: str) -> list[str] | None:
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item[0]

    def put(self, key: str, pages: list[str]) -> None:
        size = sum(len(p.encode("utf-8")) for p in pages)
        if size > self.max_bytes:
            return
        if key in self._items:
            self.size -= self._items.pop(key)[1]
        self._items[key] = (pages, size)
        self.size += size
        while self.size > self.max_bytes:
            _, (_, evicted) = self._items.popitem(last=False)
            self.size -= evicted


cache = TextCache(CACHE_MAX_BYTES)
_locks: dict[str, asyncio.Lock] = {}


def normalize_doi(doi: str) -> str:
    doi = doi.strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.IGNORECASE)
    return doi.lower()


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs) -> dict | None:
    try:
        r = await client.get(url, **kwargs)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


async def resolve_pdf_urls(client: httpx.AsyncClient, doi: str) -> list[str]:
    """Returns candidate open-access PDF URLs from Unpaywall, OpenAlex and Crossref."""
    urls: list[str] = []
    quoted = quote(doi, safe="/")

    if UNPAYWALL_EMAIL:
        data = await _get_json(
            client,
            f"https://api.unpaywall.org/v2/{quoted}",
            params={"email": UNPAYWALL_EMAIL},
        )
        if data:
            locations = [data.get("best_oa_location")] + (data.get("oa_locations") or [])
            urls += [loc["url_for_pdf"] for loc in locations if loc and loc.get("url_for_pdf")]

    data = await _get_json(client, f"https://api.openalex.org/works/doi:{quoted}")
    if data:
        locations = [data.get("best_oa_location")] + (data.get("locations") or [])
        urls += [loc["pdf_url"] for loc in locations if loc and loc.get("pdf_url")]
        oa_url = (data.get("open_access") or {}).get("oa_url")
        if oa_url:
            urls.append(oa_url)

    data = await _get_json(client, f"https://api.crossref.org/works/{quoted}")
    if data:
        for link in data.get("message", {}).get("link", []):
            if link.get("content-type") == "application/pdf" and link.get("URL"):
                urls.append(link["URL"])

    return list(dict.fromkeys(urls))


async def download_pdf(doi: str) -> bytes:
    headers = {"User-Agent": f"mcp-doi2text (mailto:{UNPAYWALL_EMAIL or 'unknown'})"}
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=HTTP_TIMEOUT, headers=headers
    ) as client:
        urls = await resolve_pdf_urls(client, doi)
        for url in urls:
            try:
                r = await client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                log.info("Downloaded PDF (%d KB) from %s", len(r.content) // 1024, url)
                return r.content
            log.info("Skipping %s (status %s, not a PDF)", url, r.status_code)
    raise ValueError(f"No open-access PDF found for DOI {doi}.")


def convert_to_markdown(content: bytes, filename: str) -> str:
    doc_format = anydoc.format_from_path(filename)
    return anydoc.to_markdown_bytes(content, format=doc_format)


def pdf_to_pages(pdf: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        pages.append(convert_to_markdown(buf.getvalue(), f"page{i}.pdf"))
    return pages


async def get_pages(doi: str) -> list[str]:
    pages = cache.get(doi)
    if pages is not None:
        log.info("Cache hit for %s (%d pages)", doi, len(pages))
        return pages
    log.info("Cache miss for %s", doi)
    lock = _locks.setdefault(doi, asyncio.Lock())
    async with lock:
        pages = cache.get(doi)
        if pages is None:
            pdf = await download_pdf(doi)
            log.info("Extracting text with anydoc...")
            pages = await asyncio.to_thread(pdf_to_pages, pdf)
            cache.put(doi, pages)
            log.info("Parsed %d pages; cache size %.1f MB", len(pages), cache.size / 1024**2)
    _locks.pop(doi, None)
    return pages


@mcp.tool()
async def get_article_by_doi(doi: str, start_page: int = 1, max_pages: int = 20) -> str:
    """Fetch the open-access PDF for a DOI and return its text as markdown.

    Args:
        doi: The DOI, e.g. "10.1038/nature12373" (a doi.org URL also works).
        start_page: First page to return (1-based).
        max_pages: Maximum number of pages to return, to page through long articles.
    """
    if start_page < 1 or max_pages < 1:
        return "start_page and max_pages must be >= 1."
    doi = normalize_doi(doi)
    log.info("Tool call: doi=%s start_page=%d max_pages=%d", doi, start_page, max_pages)
    try:
        pages = await get_pages(doi)
    except Exception as exc:
        log.error("Failed for %s: %s", doi, exc)
        return f"Failed to get text for DOI {doi}: {exc}"

    total = len(pages)
    if start_page > total:
        return f"start_page {start_page} is beyond the last page ({total})."
    end = min(start_page - 1 + max_pages, total)
    header = f"[Pages {start_page}-{end} of {total}]"
    return header + "\n\n" + "\n\n".join(pages[start_page - 1 : end])


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))
    log.info(
        "Server started (streamable-http) at http://%s:%d/mcp. Unpaywall email: %s",
        host,
        port,
        UNPAYWALL_EMAIL or "not set (Unpaywall disabled)",
    )
    mcp.run("streamable-http", host=host, port=port)
