from __future__ import annotations

from pathlib import Path


def app(environ, start_response):
    public_path = Path(__file__).with_name("public_operator.html")
    fallback_path = Path(__file__).with_name("index.html")
    body = (public_path if public_path.exists() else fallback_path).read_bytes()
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "public, max-age=60"),
    ]
    start_response("200 OK", headers)
    return [body]
