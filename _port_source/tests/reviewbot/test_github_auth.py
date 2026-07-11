import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from reviewbot.github.auth import get_installation_token, make_app_jwt

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
