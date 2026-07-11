"""GitHub App authentication: App JWT and installation tokens."""

import time

import httpx
from jose import jwt

GITHUB_API_URL = "https://api.github.com"


def make_app_jwt(client_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": client_id}
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


async def get_installation_token(
    client: httpx.AsyncClient,
    installation_id: int,
    app_jwt: str,
    api_url: str = GITHUB_API_URL,
) -> str:
    response = await client.post(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response.raise_for_status()
    return str(response.json()["token"])
