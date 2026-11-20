# MCP: PDF to text

This MCP aims to allow users the DOI of an articles and returns the extracted text from the PDF.

## Usage

```bash
pip install -r requirements.txt
echo "UNPAYWALL_EMAIL=you@example.com" > .env
python server.py        # streamable-http at http://127.0.0.1:8000/mcp (override with MCP_HOST / MCP_PORT)
pytest test_server.py   # starts the server on a free port and calls it over MCP (needs network)
```

## Design overview

Single tool: `get_article_by_doi(doi, start_page=1, max_pages=20)`.

1. **Normalize** the DOI (strips `https://doi.org/` / `doi:` prefixes, lowercases).
2. **Cache lookup**: per-page text is kept in an in-memory LRU cache keyed by DOI, capped at 100 MB (UTF-8 size). A cached DOI is not downloaded or parsed again; concurrent requests for the same DOI share one parse.
3. **Resolve** candidate open-access PDF URLs, in order: Unpaywall (requires `UNPAYWALL_EMAIL`), OpenAlex, Crossref. The first URL that returns a real PDF is used. Paywalled content is never fetched.
4. **Extract**: the PDF is split into single pages with `pypdf`, and each page is converted to markdown with `anydoc` (in a worker thread).
5. **Paginate**: pages `start_page` to `start_page + max_pages - 1` are returned, prefixed with a `[Pages X-Y of N]` header so the client can request the next range. No metadata is returned.

Limitations: the cache is lost on restart, and scanned PDFs are only handled as far as `anydoc` supports them.