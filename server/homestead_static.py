"""The app shell's files (#705): how they are cached and sent.

A script or stylesheet asked for with ?v= naming the running release (the page
versions every one) never changes under that name, so the browser keeps it for
good. The page itself never is: it is what says which release is running.
Everything else is revalidated: it carries an ETag and a browser that already
has it gets a 304 with no body. Text is sent gzipped when the browser accepts it, compressed once
and kept in memory with the file until the file changes on disk.

demo.js is made-up data for the screenshots and the live demo; a plain visit is
served the page without it, so a real install neither downloads nor parses it.
"""
import gzip
import hashlib
import os
import threading
import urllib.parse

KEEP = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
# Smaller than this is not worth a gzip header and a second copy in memory.
MIN_GZIP = 1024
COMPRESSIBLE = ("text/", "application/javascript", "application/json",
                "application/manifest+json", "image/svg+xml")
DEMO_SCRIPT = b'<script src="/js/demo.js'

_files = {}
_lock = threading.Lock()


def forget():
    """Drop every file kept in memory (tests, and a page that changed under us)."""
    with _lock:
        _files.clear()


def _without_demo(page):
    return b"".join(line for line in page.splitlines(keepends=True) if DEMO_SCRIPT not in line)


def _entry(path, plain_page):
    """The file's bytes, ETag and gzip copy, read again only when it changed on disk."""
    stat = os.stat(path)
    key, stamp = (path, plain_page), (stat.st_mtime_ns, stat.st_size)
    with _lock:
        kept = _files.get(key)
    if kept and kept["stamp"] == stamp:
        return kept
    with open(path, "rb") as handle:
        body = handle.read()
    if plain_page:
        body = _without_demo(body)
    entry = {"stamp": stamp, "body": body, "gzip": None,
             "etag": '"' + hashlib.sha256(body).hexdigest()[:24] + '"'}
    with _lock:
        _files[key] = entry
    return entry


def _wants_gzip(handler):
    accepted = handler.headers.get("Accept-Encoding", "")
    return any(part.split(";")[0].strip() == "gzip" for part in accepted.split(","))


def _matches(entry, wanted):
    """If-None-Match: weak tags match too (a proxy that re-encodes weakens them), and *."""
    tags = [tag.strip() for tag in wanted.split(",") if tag.strip()]
    return "*" in tags or entry["etag"] in [tag[2:] if tag.startswith("W/") else tag for tag in tags]


def serve(handler, path, ctype, cache="", version=""):
    """Send one static file. cache overrides the policy for files that set their own
    (icons, the service worker); a file asked for with ?v= naming the running
    version is kept for good either way."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    page = ctype.startswith("text/html")
    plain_page = page and (query.get("demo") or [""])[0] != "1"
    try:
        entry = _entry(path, plain_page)
    except (FileNotFoundError, NotADirectoryError):
        return handler._send(404, {"error": "not found"})
    policy = KEEP if not page and version and (query.get("v") or [""])[0] == version else (cache or REVALIDATE)
    compressible = ctype.startswith(COMPRESSIBLE) and len(entry["body"]) >= MIN_GZIP
    headers = [("Cache-Control", policy), ("ETag", entry["etag"])]
    if compressible:
        headers.append(("Vary", "Accept-Encoding"))
    if _matches(entry, handler.headers.get("If-None-Match", "")):
        handler.send_response(304)
        for name, value in list(getattr(handler, "_extra_headers", None) or []) + headers:
            handler.send_header(name, value)
        handler._security_headers()
        handler.end_headers()
        return None
    body = entry["body"]
    if compressible and _wants_gzip(handler):
        if entry["gzip"] is None:
            entry["gzip"] = gzip.compress(body, compresslevel=6, mtime=0)
        body = entry["gzip"]
        headers.append(("Content-Encoding", "gzip"))
    handler._extra_headers = list(getattr(handler, "_extra_headers", None) or []) + headers
    return handler._send(200, body, ctype)
