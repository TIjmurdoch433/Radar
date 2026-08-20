#!/usr/bin/env python3
"""
Local proxy for the Sustainability Technology Radar's "Latest from Feedly" panel.

Why this exists: Feedly's Cloud API sends no CORS headers, so a browser can never
call it directly, and a personal access token must never be shipped inside
client-side JavaScript (anyone viewing the page could read and reuse it). This
script holds the key server-side and exposes one narrow local endpoint that the
radar's index.html calls instead.

Setup:
    export FEEDLY_API_KEY="your-feedly-access-token"
    python3 feedly_proxy.py

Then open index.html (or reload it if already open) with the proxy still running.
The radar calls http://localhost:8787/api/articles?query=<technology name> and the
proxy does the two Feedly calls (find matching feeds, then fetch their recent
items) on the server side, returning a small merged JSON list.

Never commit a real key into this file or into git. Set it via the environment
variable each time you run this, or put it in a local .env-style shell profile
that isn't checked into version control.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FEEDLY_API_KEY = os.environ.get("FEEDLY_API_KEY", "").strip()
PORT = int(os.environ.get("FEEDLY_PROXY_PORT", "8787"))
CACHE_TTL_SECONDS = 600
FEED_SEARCH_COUNT = 3
ARTICLES_PER_FEED = 6
MAX_ARTICLES_RETURNED = 6

_cache = {}


def feedly_get(path, params):
    url = "https://cloud.feedly.com" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "OAuth " + FEEDLY_API_KEY})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def strip_html(html):
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 220:
        text = text[:217].rstrip() + "..."
    return text


def fetch_articles(query):
    now = time.time()
    cached = _cache.get(query)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    feeds = feedly_get("/v3/search/feeds", {"query": query, "n": FEED_SEARCH_COUNT}).get("results", [])

    seen_ids = set()
    merged = []
    for feed in feeds:
        feed_id = feed.get("id")
        if not feed_id:
            continue
        try:
            contents = feedly_get(
                "/v3/streams/contents",
                {"streamId": feed_id, "count": ARTICLES_PER_FEED},
            )
        except urllib.error.URLError:
            continue
        for item in contents.get("items", []):
            item_id = item.get("id")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            alt = item.get("alternate") or []
            url = alt[0]["href"] if alt and "href" in alt[0] else item.get("canonicalUrl", "")
            if not url:
                continue
            origin = item.get("origin") or {}
            summary_html = (item.get("summary") or {}).get("content", "")
            published_ms = item.get("published") or item.get("crawled") or 0
            merged.append(
                {
                    "title": item.get("title") or "(untitled)",
                    "url": url,
                    "source": origin.get("title") or feed.get("title") or "Unknown source",
                    "published": published_ms,
                    "summary": strip_html(summary_html),
                }
            )

    merged.sort(key=lambda a: a["published"], reverse=True)
    top = merged[:MAX_ARTICLES_RETURNED]
    for a in top:
        a["published"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(a["published"] / 1000))
            if a["published"]
            else None
        )

    _cache[query] = (now, top)
    return top


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/articles":
            self._send_json(404, {"error": "not_found"})
            return

        if not FEEDLY_API_KEY:
            self._send_json(
                500,
                {"error": "missing_api_key", "message": "Set the FEEDLY_API_KEY environment variable before starting this proxy."},
            )
            return

        qs = urllib.parse.parse_qs(parsed.query)
        query = (qs.get("query") or [""])[0].strip()
        if not query:
            self._send_json(400, {"error": "missing_query"})
            return

        try:
            articles = fetch_articles(query)
            self._send_json(200, {"query": query, "articles": articles})
        except urllib.error.HTTPError as e:
            self._send_json(502, {"error": "feedly_http_error", "status": e.code})
        except Exception as e:
            self._send_json(502, {"error": "feedly_unreachable", "message": str(e)})

    def log_message(self, fmt, *args):
        pass


def main():
    if not FEEDLY_API_KEY:
        print("WARNING: FEEDLY_API_KEY is not set. Requests will return an error until you set it.")
        print('Run: export FEEDLY_API_KEY="your-token" then restart this script.')
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Feedly proxy running at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
