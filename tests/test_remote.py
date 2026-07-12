import httpx
import pytest

from themis.engines import EngineError, EngineQuotaError, EngineUnavailableError
from themis.remote import RemoteEngine


async def test_run_non_json_failure_maps_to_engine_error(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text="upstream exploded")
    )
    engine = RemoteEngine("claude", "http://agent", "secret", transport=transport)

    with pytest.raises(EngineError, match="non-JSON.*upstream exploded"):
        await engine.run(
            prompt="review", workspace=tmp_path, model="opus", effort="high",
            timeout=10,
        )


async def test_run_success_returns_output(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"output": "done"})
    )
    engine = RemoteEngine("claude", "http://agent", "secret", transport=transport)

    assert await engine.run(
        prompt="review", workspace=tmp_path, model="opus", effort="high", timeout=10,
    ) == "done"


async def test_generic_json_503_stays_engine_error(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"detail": "proxy unavailable"})
    )
    engine = RemoteEngine("claude", "http://agent", "secret", transport=transport)

    with pytest.raises(EngineError, match="proxy unavailable") as exc_info:
        await engine.run(
            prompt="review", workspace=tmp_path, model="opus", effort="high", timeout=10,
        )
    assert not isinstance(exc_info.value, EngineUnavailableError)


async def test_credential_error_code_maps_to_engine_unavailable(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json={
        "detail": {
            "code": "engine_credentials_unavailable",
            "message": "claude credentials unavailable",
        }
    }))
    engine = RemoteEngine("claude", "http://agent", "secret", transport=transport)

    with pytest.raises(EngineUnavailableError, match="credentials unavailable"):
        await engine.run(
            prompt="review", workspace=tmp_path, model="opus", effort="high", timeout=10,
        )


async def test_run_non_object_json_maps_to_engine_error(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=["not", "an", "object"])
    )
    engine = RemoteEngine("claude", "http://agent", "secret", transport=transport)

    with pytest.raises(EngineError, match="invalid JSON response"):
        await engine.run(
            prompt="review", workspace=tmp_path, model="opus", effort="high", timeout=10,
        )


async def test_run_429_maps_to_quota_error(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, json={"detail": "usage limit reached"})
    )
    engine = RemoteEngine("claude", "http://agent", "secret", transport=transport)

    with pytest.raises(EngineQuotaError, match="usage limit reached"):
        await engine.run(
            prompt="review", workspace=tmp_path, model="opus", effort="high", timeout=10,
        )


async def test_run_sends_bearer_token_and_workspace_name(tmp_path):
    seen = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["payload"] = request.read()
        return httpx.Response(200, json={"output": "done"})

    engine = RemoteEngine(
        "claude", "http://agent", "secret", transport=httpx.MockTransport(capture)
    )
    await engine.run(
        prompt="review", workspace=tmp_path / "job123", model="opus", effort="high",
        timeout=10,
    )
    assert seen["auth"] == "Bearer secret"
    assert b'"workspace":"job123"' in seen["payload"]
