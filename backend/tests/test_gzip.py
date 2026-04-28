"""Tests for GZipMiddleware added in perf/gzip-middleware.

Validates Starlette's built-in GZipMiddleware behaves as configured on a
minimal FastAPI app:

  1. Large JSON response with `accept-encoding: gzip` → gzipped.
  2. Same endpoint without that header → no `content-encoding`.
  3. Tiny response (< 1024 bytes, e.g. /health) → NOT gzipped even with
     `accept-encoding: gzip` (minimum_size=1024 threshold).

We construct a minimal FastAPI app rather than importing app.main (which
boots Sentry + the candidate engine). The middleware-under-test is
Starlette's, so wiring it onto a tiny app exercises the same code path
production runs.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_gzip.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

# Match the convention used in other backend tests so this file runs
# standalone too.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _build_app() -> FastAPI:
    app = FastAPI()
    # Identical configuration to backend/app/main.py.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/health")
    def health():
        # Tiny payload — well under 1024 bytes.
        return {"ok": True}

    @app.get("/big")
    def big():
        # Repeating JSON-friendly payload guaranteed to exceed 1 KB.
        return {"items": [{"i": i, "label": "pulse-card"} for i in range(200)]}

    return app


def test_gzip_applied_when_accept_encoding_set():
    client = TestClient(_build_app())
    r = client.get("/big", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_gzip_skipped_when_accept_encoding_absent():
    # TestClient defaults include `accept-encoding: gzip, deflate`. We
    # need to explicitly clear it to simulate a client that doesn't
    # advertise gzip support.
    client = TestClient(_build_app())
    r = client.get("/big", headers={"accept-encoding": "identity"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") != "gzip"


def test_gzip_skipped_for_small_response():
    # /health returns ~13 bytes of JSON — well below the 1024-byte
    # threshold, so GZipMiddleware should pass it through unchanged
    # even when the client advertises gzip support.
    client = TestClient(_build_app())
    r = client.get("/health", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") != "gzip"
