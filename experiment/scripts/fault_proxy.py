#!/usr/bin/env python3
"""Deterministic Visits fault proxy with a control plane isolated from traffic."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BACKEND_URL = os.environ.get("BACKEND_URL", "http://visits-service:8082").rstrip("/")
DATA_PORT = int(os.environ.get("DATA_PORT", "8082"))
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8474"))
JOURNAL_PATH = Path(os.environ.get("JOURNAL_PATH", "/artifacts/fault-journal.jsonl"))
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
}


class FaultState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enabled = False
        self.injected = 0
        self.forwarded = 0
        self.epoch = 0

    def set_enabled(self, enabled: bool, reason: str) -> dict[str, object]:
        with self._lock:
            self.enabled = enabled
            self.epoch += 1
            snapshot = self.snapshot_unlocked()
        journal({"event": "fault-toggle", "reason": reason, **snapshot})
        return snapshot

    def record(self, injected: bool) -> tuple[bool, int]:
        with self._lock:
            active = self.enabled
            if injected:
                self.injected += 1
                count = self.injected
            else:
                self.forwarded += 1
                count = self.forwarded
        return active, count

    def snapshot_unlocked(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "injected": self.injected,
            "forwarded": self.forwarded,
            "epoch": self.epoch,
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self.snapshot_unlocked()


STATE = FaultState()
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
        if STATE.snapshot()["enabled"]:
            _, ordinal = STATE.record(injected=True)
            journal({"event": "injected-503", "ordinal": ordinal, "path": self.path})
            send_json(self, 503, {"error": "experiment fault", "status": 503})
            return

        STATE.record(injected=False)
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS
        }
        request = urllib.request.Request(
            BACKEND_URL + self.path,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(request, timeout=4.0)
        except urllib.error.HTTPError as error:
            response = error
        except Exception as error:  # backend health is validated separately
            send_json(self, 502, {"error": type(error).__name__, "status": 502})
            return

        response_body = response.read()
        self.send_response(response.status)
        for key, value in response.headers.items():
            if key.lower() not in HOP_HEADERS:
                self.send_header(key, value)
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
        if self.path == "/status":
            send_json(self, 200, STATE.snapshot())
        else:
            send_json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/fault/on":
            send_json(self, 200, STATE.set_enabled(True, "control-api"))
        elif self.path == "/fault/off":
            send_json(self, 200, STATE.set_enabled(False, "control-api"))
        else:
            send_json(self, 404, {"error": "not found"})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    journal({"event": "proxy-start", "backend": BACKEND_URL})
    control = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", DATA_PORT), DataHandler).serve_forever()


if __name__ == "__main__":
    main()

