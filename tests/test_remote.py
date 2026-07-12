import httpx
import pytest

from themis.engines import EngineError
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
