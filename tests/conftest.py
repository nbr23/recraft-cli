"""Shared fixtures: a local stub of the Recraft API.

Nothing here touches the network or needs a token, so the suite is free to run.
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import NamedTuple

import pytest

TOKEN = "SECRET-TOKEN-DO-NOT-LEAK"


def make_png(width: int, height: int, rgba: list[int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_webp() -> bytes:
    """Enough of a VP8L header for format sniffing; not a decodable image."""
    payload = b"VP8L" + struct.pack("<I", 12) + b"\x2f" + b"\x00" * 11
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload


PNG = make_png(1024, 1024, [10, 20, 30, 255])
PNG_ALPHA = make_png(512, 512, [10, 20, 30, 0])
WEBP = make_webp()
SVG = (
    b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
    b'width="1024" height="768"><rect width="10" height="10"/></svg>'
)

STATE: dict = {"requests": [], "fail_429": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def route(self) -> str:
        return self.path.split("?", 1)[0]

    def _send(self, code: int, payload, ctype: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, body=None) -> None:
        STATE["requests"].append({"method": method, "path": self.path, "body": body or {}})

    def do_GET(self):
        self._record("GET")
        if self.route.startswith("/asset/"):
            if self.route.endswith(".svg"):
                return self._send(200, SVG, "image/svg+xml")
            if "webp" in self.route:
                return self._send(200, WEBP, "image/webp")
            if "alpha" in self.route:
                return self._send(200, PNG_ALPHA, "image/png")
            return self._send(200, PNG, "image/png")
        if self.route.startswith("/v1/users/me"):
            if self.headers.get("Authorization") == "Bearer BADTOKEN":
                return self._send(401, {"code": "unauthorized", "message": "invalid token"})
            return self._send(200, {"id": "u1", "credits": 12345, "email": "x@y.z"})
        if self.route == "/v1/models":
            return self._send(200, {"data": [
                {"id": "recraftv4_1", "name": "Recraft V4.1", "created": 1,
                 "input_modalities": ["text"], "output_modalities": ["image"],
                 "pricing": {"image": "0.035"}},
                {"id": "gpt_image_1_low", "name": "GPT Image", "created": 1,
                 "input_modalities": ["text"], "output_modalities": ["image"],
                 "pricing": {"image": "0.02"}},
            ]})
        if self.route == "/v1/styles/basic":
            return self._send(200, {"styles": [
                {"model": "recraftv4_1", "style": "realistic_image", "style_id": "s1"},
                {"model": "recraftv4_1", "style": "digital_illustration", "style_id": "s2"},
            ]})
        if self.route == "/v1/styles":
            return self._send(200, {"styles": []})
        return self._send(404, {"code": "not_found", "message": self.path})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        content_type = self.headers.get("Content-Type", "")
        body = {}
        if content_type.startswith("application/json"):
            body = json.loads(raw or b"{}")
        elif raw:
            # Crude multipart field scrape, enough to assert on simple values.
            for part in raw.split(b"--")[1:]:
                if b'name="' in part and b"\r\n\r\n" in part:
                    name = part.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
                    value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
                    if b"filename=" not in part:
                        body[name] = value.decode("utf-8", "replace")
        self._record("POST", body)

        if STATE["fail_429"] > 0:
            STATE["fail_429"] -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if body.get("model") == "nosuchmodel":
            return self._send(400, {"code": "invalid_model",
                                    "message": "model nosuchmodel is unknown"})
        if body.get("prompt") == "BROKE":
            return self._send(402, {"code": "not_enough_credits",
                                    "message": "insufficient credits"})
        if body.get("prompt") == "NOURL":
            return self._send(200, {"created": 1, "credits": 35,
                                    "data": [{"image_id": "i1", "b64_json": "AAAA" * 5000}]})

        host = f"http://127.0.0.1:{PORT}"
        fmt = body.get("image_format", "png")
        n = int(body.get("n", 1))

        if self.route.endswith("/vectorize"):
            return self._send(200, {"created": 1, "credits": 10,
                                    "image": {"image_id": "v1", "url": f"{host}/asset/1.svg"}})
        if self.route.endswith("/removeBackground"):
            return self._send(200, {"created": 1, "credits": 10,
                                    "image": {"image_id": "r1", "url": f"{host}/asset/alpha.png"}})
        if self.route.endswith("/crispUpscale"):
            return self._send(200, {"created": 1, "credits": 4,
                                    "image": {"image_id": "u1", "url": f"{host}/asset/1.png"}})
        if self.route.endswith("/prompts/enhance"):
            return self._send(200, {"enhanced_prompt": "a vivid red bicycle, studio light",
                                    "credits": 2})
        if self.route == "/v1/styles":
            return self._send(200, {"id": "style-abc-123", "style": "digital_illustration",
                                    "creation_time": "2026-01-01T00:00:00Z",
                                    "is_private": False, "credits": 40})
        if self.route.endswith("/generations/vector"):
            return self._send(200, {"created": 1, "credits": 80,
                                    "data": [{"image_id": "g1", "url": f"{host}/asset/1.svg"}]})

        # Honour the requested format so the extension/content check is real.
        asset = "webp.png" if fmt == "webp" else "{i}.png"
        return self._send(200, {"created": 1, "credits": 35 * n, "data": [
            {"image_id": f"img{i}", "url": f"{host}/asset/" + asset.format(i=i),
             "revised_prompt": "a red bicycle, detailed"} for i in range(n)]})

    def do_DELETE(self):
        self._record("DELETE")
        return self._send(200, {})


_server = HTTPServer(("127.0.0.1", 0), Handler)
PORT = _server.server_address[1]
threading.Thread(target=_server.serve_forever, daemon=True).start()


class Result(NamedTuple):
    code: int
    out: str
    err: str


@pytest.fixture
def stub():
    STATE["requests"].clear()
    STATE["fail_429"] = 0
    return STATE


@pytest.fixture
def rc(monkeypatch, tmp_path):
    import recraft

    monkeypatch.setattr(recraft, "BASE_URL", f"http://127.0.0.1:{PORT}")
    monkeypatch.setenv("RECRAFT_API_TOKEN", TOKEN)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return recraft


@pytest.fixture
def run(rc, stub):
    def _run(*argv: str) -> Result:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = rc.main(list(argv))
            except SystemExit as exc:  # argparse exits directly on bad arguments
                code = exc.code if isinstance(exc.code, int) else 2
        return Result(code, out.getvalue(), err.getvalue())

    return _run


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "in.png"
    path.write_bytes(make_png(64, 48, [200, 30, 30, 255]))
    return str(path)


@pytest.fixture
def mask(tmp_path):
    path = tmp_path / "mask.png"
    path.write_bytes(make_png(64, 48, [255, 255, 255, 255]))
    return str(path)


def posts(state) -> list[dict]:
    return [r for r in state["requests"] if r["method"] == "POST"]


def last_body(state) -> dict:
    return posts(state)[-1]["body"]
