import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from themis.github.auth import (
    get_app_slug,
    get_installation_token,
    get_repo_installation_id,
    make_app_jwt,
    update_webhook_url,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_make_app_jwt__valid_key__contains_expected_claims(rsa_pem: str):
    token = make_app_jwt(client_id="Iv1.abc", private_key_pem=rsa_pem)

    public_pem = (
        serialization.load_pem_private_key(rsa_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    # decode verifies the RS256 signature against the public key
    claims = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert claims["iss"] == "Iv1.abc"
    # iat backdated 60s for clock drift; GitHub caps lifetime at 10 minutes
    assert claims["exp"] - claims["iat"] == 600
    assert claims["iat"] <= int(time.time()) - 59


async def test_get_installation_token__created__returns_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.url.path == "/app/installations/42/access_tokens"
        assert request.headers["Authorization"] == "Bearer app-jwt"
        return httpx.Response(201, json={"token": "ghs_abc"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await get_installation_token(client, installation_id=42, app_jwt="app-jwt")

    assert token == "ghs_abc"


async def test_get_installation_token__unauthorized__raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_installation_token(client, installation_id=42, app_jwt="app-jwt")


async def test_get_app_slug():
    def handler(request):
        assert request.url.path == "/app"
        assert request.headers["Authorization"] == "Bearer jwt-123"
        return httpx.Response(200, json={"id": 1, "slug": "my-reviewer"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        slug = await get_app_slug(client, "jwt-123", api_url="https://api.test")
    assert slug == "my-reviewer"


async def test_get_repo_installation_id():
    def handler(request):
        assert request.url.path == "/repos/acme/widgets/installation"
        return httpx.Response(200, json={"id": 42})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        installation_id = await get_repo_installation_id(
            client, "acme/widgets", "jwt-123", api_url="https://api.test"
        )
    assert installation_id == 42


async def test_get_repo_installation_id_not_installed():
    def handler(request):
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        installation_id = await get_repo_installation_id(
            client, "acme/widgets", "jwt-123", api_url="https://api.test"
        )
    assert installation_id is None


async def test_update_webhook_url():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"url": "https://x/webhook"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await update_webhook_url(client, "https://x/webhook", "jwt-123", api_url="https://api.test")
    assert seen == {"method": "PATCH", "path": "/app/hook/config", "json": {"url": "https://x/webhook"}}
