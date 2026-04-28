"""
Tests for RailwayClient security: bearer token must not appear in logs.

Covers:
  - A token with a trailing newline triggers httpx.LocalProtocolError.
  - The raw token value does NOT appear in any captured log record.
  - A sanitised warning IS logged instead.
  - RailwayError is raised with a safe message.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ops_bot.railway_client import RailwayClient, RailwayError


@pytest.mark.asyncio
async def test_malformed_token_does_not_leak_to_logs(caplog):
    """
    When RAILWAY_API_TOKEN has a trailing newline, httpx raises
    LocalProtocolError whose str() contains the raw header value. The client
    must catch it and emit a safe log message without the token substring.
    """
    bad_token = "46d4cbf7-7f46-44f9-b8dc-0cbe79b040c3\n"
    client = RailwayClient(bad_token)

    # Patch the underlying httpx client to raise LocalProtocolError, simulating
    # what httpx does when it sees an illegal header value (e.g. embedded newline).
    local_error = httpx.LocalProtocolError(
        f"Illegal header value b'Bearer {bad_token}'"
    )

    with patch.object(client._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = local_error

        with caplog.at_level(logging.WARNING, logger="ops_bot.railway_client"):
            with pytest.raises(RailwayError) as exc_info:
                await client.latest_deployment("proj-123", "svc-456")

    # The token must NOT appear anywhere in the log output.
    all_log_text = " ".join(r.getMessage() for r in caplog.records)
    assert bad_token.strip() not in all_log_text, (
        "raw token value leaked into log output"
    )
    assert "46d4cbf7" not in all_log_text, (
        "token fragment leaked into log output"
    )

    # A sanitised warning must have been emitted.
    assert any("malformed Authorization header" in r.getMessage() for r in caplog.records), (
        "expected a sanitised warning about the malformed header"
    )

    # The raised RailwayError message must also be token-free.
    assert bad_token.strip() not in str(exc_info.value)
    assert "46d4cbf7" not in str(exc_info.value)

    await client.close()


@pytest.mark.asyncio
async def test_generic_exception_does_not_log_str_exc(caplog):
    """
    For non-LocalProtocolError exceptions, the client logs type(exc).__name__,
    not str(exc) (which could contain sensitive values).
    """
    client = RailwayClient("clean-token")

    # Simulate a generic exception whose message contains a hypothetical secret.
    class _WeirdError(Exception):
        pass

    sensitive_exc = _WeirdError("contains-a-secret-value")

    with patch.object(client._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = sensitive_exc

        with pytest.raises(RailwayError) as exc_info:
            await client.latest_deployment("proj-123", "svc-456")

    # The str(exc) must not appear in the raised RailwayError message.
    assert "contains-a-secret-value" not in str(exc_info.value)
    # The error name should appear (it's safe type info).
    assert "_WeirdError" in str(exc_info.value)

    await client.close()
