#!/usr/bin/env python3
"""Deterministic 99:1 router with a runtime-selected minority gateway."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


GATEWAYS = {
    "A": os.environ.get("GATEWAY_A_URL", "http://gateway-a:8080").rstrip("/"),
    "B": os.environ.get("GATEWAY_B_URL", "http://gateway-b:8080").rstrip("/"),
}
DATA_PORT = int(os.environ.get("DATA_PORT", "8080"))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8475"))
MINORITY_GATEWAY = os.environ.get("MINORITY_GATEWAY", "B").upper()
MINORITY_EVERY = int(os.environ.get("MINORITY_EVERY", "100"))
if MINORITY_GATEWAY not in GATEWAYS:
    raise ValueError(f"MINORITY_GATEWAY must be one of {sorted(GATEWAYS)}")
JOURNAL_PATH = Path(os.environ.get("JOURNAL_PATH", "/artifacts/router-journal.jsonl"))
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "x-experiment-target",
}


class RouteState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ordinal = 0
        self.a = 0
        self.b = 0
        self.epoch = 0

    def select(self, forced: str | None) -> tuple[str, int, int]:
        with self._lock:
            self.ordinal += 1
            if forced in GATEWAYS:
                selected = forced
            else:
                minority = self.ordinal % MINORITY_EVERY == 0
                selected = MINORITY_GATEWAY if minority else next(
                    gateway for gateway in GATEWAYS if gateway != MINORITY_GATEWAY
                )
            if selected == "A":
                self.a += 1
            else:
                self.b += 1
            return selected, self.ordinal, self.epoch

    def reset(self) -> dict[str, int]:
        with self._lock:
            self.ordinal = 0
            self.a = 0
            self.b = 0
            self.epoch += 1
            return self.snapshot_unlocked()

    def snapshot_unlocked(self) -> dict[str, int]:
        return {"ordinal": self.ordinal, "A": self.a, "B": self.b, "epoch": self.epoch}

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return self.snapshot_unlocked()


STATE = RouteState()
JOURNAL_LOCK = threading.Lock()


def journal(record: dict[str, object]) -> None:
    record = {"unix_ns": time.time_ns(), **record}
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL_LOCK, JOURNAL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class DataHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        forced = self.headers.get("X-Experiment-Target", "").upper() or None
        selected, ordinal, epoch = STATE.select(forced)
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS
        }
        request = urllib.request.Request(
            GATEWAYS[selected] + self.path,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(request, timeout=6.0)
        except urllib.error.HTTPError as error:
            response = error
        except Exception as error:
            send_json(self, 502, {"error": type(error).__name__, "gateway": selected})
            return

        response_body = response.read()
        self.send_response(response.status)
        for key, value in response.headers.items():
            if key.lower() not in HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("X-Experiment-Gateway", selected)
        self.send_header("X-Experiment-Route-Epoch", str(epoch))
        self.send_header("X-Experiment-Route-Ordinal", str(ordinal))
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ControlHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/stats":
            send_json(self, 200, STATE.snapshot())
        else:
            send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/reset":
            snapshot = STATE.reset()
            journal({"event": "router-reset", **snapshot})
            send_json(self, 200, snapshot)
        else:
            send_json(self, 404, {"error": "not found"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    journal(
        {
            "event": "router-start",
            "minority_gateway": MINORITY_GATEWAY,
            "minority_every": MINORITY_EVERY,
        }
    )
    control = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", DATA_PORT), DataHandler).serve_forever()


if __name__ == "__main__":
    main()
