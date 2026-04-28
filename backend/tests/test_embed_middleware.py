"""Tests for the EmbedTokenMiddleware + match_origin helper.

Covers the four acceptance behaviours from the PR brief:

  1. Kill switch off → /api/feed always passes (no enforcement).
  2. Kill switch on:
       - missing token → 401
       - bad token → 401
       - good token + allowed origin → pass
       - good token + disallowed origin → 403
  3. The wildcard prefix rule: `*.example.com` matches subdomains but
     NOT the bare domain.
  4. The localhost shortcut.

Mocks the FastAPI app — we don't spin up the real /api/feed handler.
A bare ASGI placeholder is enough to verify the middleware decision.

Run with:

    cd ~/pulse-poc/backend
    venv/bin/python -m pytest tests/test_embed_middleware.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Make backend/app importable when invoked as `pytest tests/...` from
# inside backend/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.embeds import EmbedTokenMiddleware, match_origin
from app.services.candidate_store import CandidateStore


# ─────────────────────────────────────────────────────────────────────────
# match_origin — pure function, no DB needed
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "host,patterns,expected",
    [
        # Localhost shortcut
        ("localhost", ["localhost"], True),
        ("127.0.0.1", ["localhost"], True),
        ("::1", ["localhost"], True),
        ("localhost", ["example.com"], False),
        # Literal match
        ("apuestatotal.pe", ["apuestatotal.pe"], True),
        ("apuestatotal.pe", ["apuestatotal.com"], False),
        ("APUESTATOTAL.PE", ["apuestatotal.pe"], True),  # case-insensitive
        # Wildcard prefix — subdomain matches
        ("sub.apuestatotal.com.pe", ["*.apuestatotal.com.pe"], True),
        ("deep.sub.apuestatotal.com.pe", ["*.apuestatotal.com.pe"], True),
        # Wildcard prefix — bare domain does NOT match
        ("apuestatotal.com.pe", ["*.apuestatotal.com.pe"], False),
        # Wildcard prefix — unrelated domain
        ("evil.com", ["*.apuestatotal.com.pe"], False),
        # Multiple patterns; matches one
        (
            "apuestatotal.pe",
            ["*.apuestatotal.com.pe", "apuestatotal.pe", "localhost"],
            True,
        ),
        # Empty host → never matches
        ("", ["localhost"], False),
        # No patterns → never matches
        ("foo.com", [], False),
    ],
)
def test_match_origin(host, patterns, expected):
    assert match_origin(host, patterns) is expected


# ─────────────────────────────────────────────────────────────────────────
# Middleware harness — ASGI plumbing
# ─────────────────────────────────────────────────────────────────────────

class _DummyApp:
    """Trivial ASGI app that records that it was called and returns 200."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"ok":true}',
        })


class _SendCollector:
    """Captures the messages an ASGI app sends so tests can assert on
    them without spinning up a real server."""

    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self) -> int:
        for m in self.messages:
            if m.get("type") == "http.response.start":
                return int(m.get("status", 0))
        return 0

    @property
    def body(self) -> bytes:
        out = b""
        for m in self.messages:
            if m.get("type") == "http.response.body":
                out += m.get("body", b"")
        return out

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return {}


def _scope(
    *,
    path: str = "/api/feed",
    method: str = "GET",
    query_string: bytes = b"",
    origin: str = "",
    referer: str = "",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
):
    headers: list[tuple[bytes, bytes]] = []
    if origin:
        headers.append((b"origin", origin.encode("latin-1")))
    if referer:
        headers.append((b"referer", referer.encode("latin-1")))
    if extra_headers:
        headers.extend(extra_headers)
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": headers,
    }


async def _noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


# ─────────────────────────────────────────────────────────────────────────
# Middleware behaviour
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_kill_switch_passes_through():
    """When PULSE_EMBED_TOKEN_REQUIRED=false, every /api/feed request
    is forwarded to the inner app, regardless of token / origin."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=False)

        send = _SendCollector()
        # No token, no origin — would 401 if enforcement were on.
        await mw(_scope(), _noop_receive, send)

        assert inner.called is True
        assert send.status == 200


@pytest.mark.asyncio
async def test_enabled_missing_token_returns_401():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        await mw(_scope(origin="https://op.example.com"), _noop_receive, send)

        assert inner.called is False
        assert send.status == 401
        assert send.json()["detail"] == "embed_token required"


@pytest.mark.asyncio
async def test_enabled_bad_token_returns_401():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["op.example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        await mw(
            _scope(
                query_string=b"embed_token=BOGUS",
                origin="https://op.example.com",
            ),
            _noop_receive, send,
        )

        assert inner.called is False
        assert send.status == 401
        assert send.json()["detail"] == "embed_token invalid"


@pytest.mark.asyncio
async def test_enabled_good_token_allowed_origin_passes():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["*.example.com", "example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        qs = f"embed_token={embed.token}".encode("latin-1")
        await mw(
            _scope(query_string=qs, origin="https://sub.example.com"),
            _noop_receive, send,
        )

        assert inner.called is True
        assert send.status == 200


@pytest.mark.asyncio
async def test_enabled_good_token_disallowed_origin_returns_403():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["*.example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        qs = f"embed_token={embed.token}".encode("latin-1")
        await mw(
            _scope(query_string=qs, origin="https://evil.com"),
            _noop_receive, send,
        )

        assert inner.called is False
        assert send.status == 403
        assert send.json()["detail"] == "origin not allowed"


@pytest.mark.asyncio
async def test_inactive_token_returns_401():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["op.example.com"],
        )
        await store.soft_delete_embed("op")

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        qs = f"embed_token={embed.token}".encode("latin-1")
        await mw(
            _scope(query_string=qs, origin="https://op.example.com"),
            _noop_receive, send,
        )

        assert inner.called is False
        assert send.status == 401
        assert send.json()["detail"] == "embed_token invalid"


@pytest.mark.asyncio
async def test_header_token_works_when_query_param_missing():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["op.example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        await mw(
            _scope(
                origin="https://op.example.com",
                extra_headers=[
                    (b"x-pulse-embed-token", embed.token.encode("latin-1")),
                ],
            ),
            _noop_receive, send,
        )

        assert inner.called is True
        assert send.status == 200


@pytest.mark.asyncio
async def test_referer_used_when_origin_missing():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["op.example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        qs = f"embed_token={embed.token}".encode("latin-1")
        await mw(
            _scope(
                query_string=qs,
                referer="https://op.example.com/page",
                # No Origin header — should fall back to Referer.
            ),
            _noop_receive, send,
        )

        assert inner.called is True
        assert send.status == 200


@pytest.mark.asyncio
async def test_no_origin_no_referer_returns_403():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["op.example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        qs = f"embed_token={embed.token}".encode("latin-1")
        await mw(_scope(query_string=qs), _noop_receive, send)

        assert inner.called is False
        assert send.status == 403
        assert send.json()["detail"] == "origin required"


@pytest.mark.asyncio
async def test_localhost_shortcut_passes():
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()
        embed = await store.create_embed(
            slug="op", display_name="Op",
            allowed_origins=["localhost", "*.example.com"],
        )

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        qs = f"embed_token={embed.token}".encode("latin-1")
        await mw(
            _scope(query_string=qs, origin="http://localhost:5173"),
            _noop_receive, send,
        )

        assert inner.called is True
        assert send.status == 200


@pytest.mark.asyncio
async def test_non_feed_path_passes_through_when_enabled():
    """Even with enforcement on, /admin/* and / are unaffected — the
    middleware only gates /api/feed and /api/feed/*."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        # /admin/embeds — passes through with no token.
        send = _SendCollector()
        await mw(_scope(path="/admin/embeds"), _noop_receive, send)
        assert inner.called is True
        assert send.status == 200

        # /reactions — also passes through.
        inner.called = False
        send = _SendCollector()
        await mw(_scope(path="/api/cards/abc/react", method="POST"), _noop_receive, send)
        assert inner.called is True
        assert send.status == 200


@pytest.mark.asyncio
async def test_post_to_feed_passes_through():
    """The middleware only intercepts GET / HEAD — a POST (e.g. some
    future write surface) is not gated. Documented to make the surface
    explicit; callers should still gate writes via their own auth."""
    with tempfile.TemporaryDirectory() as tmp:
        store = CandidateStore(str(Path(tmp) / "pulse.db"))
        await store.init()

        inner = _DummyApp()
        mw = EmbedTokenMiddleware(inner, store, enabled=True)

        send = _SendCollector()
        await mw(
            _scope(path="/api/feed", method="POST"), _noop_receive, send,
        )
        assert inner.called is True
        assert send.status == 200
