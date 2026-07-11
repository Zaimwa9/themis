"""GitHub App authentication: App JWT and installation tokens."""

import time

import httpx
from jose import jwt

GITHUB_API_URL = "https://api.github.com"


def make_app_jwt(client_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": client_id}
    return jwt.encode(claims, private_key_pem, algorithm="RS256")


def _app_headers(app_jwt: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_installation_token(
    client: httpx.AsyncClient,
    installation_id: int,
    app_jwt: str,
    api_url: str = GITHUB_API_URL,
) -> str:
    response = await client.post(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        headers=_app_headers(app_jwt),
    )
    response.raise_for_status()
    return str(response.json()["token"])


async def get_app_slug(
    client: httpx.AsyncClient, app_jwt: str, api_url: str = GITHUB_API_URL
) -> str:
    response = await client.get(f"{api_url}/app", headers=_app_headers(app_jwt))
    response.raise_for_status()
    return str(response.json()["slug"])


async def get_repo_installation_id(
    client: httpx.AsyncClient, repo: str, app_jwt: str, api_url: str = GITHUB_API_URL
) -> int | None:
    """Installation id of this App on `repo`, or None when not installed."""
    response = await client.get(
        f"{api_url}/repos/{repo}/installation", headers=_app_headers(app_jwt)
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return int(response.json()["id"])


async def update_webhook_url(
    client: httpx.AsyncClient, url: str, app_jwt: str, api_url: str = GITHUB_API_URL
) -> None:
    """Point the App's webhook at `url` (PATCH /app/hook/config)."""
    response = await client.patch(
        f"{api_url}/app/hook/config", headers=_app_headers(app_jwt), json={"url": url}
    )
    response.raise_for_status()
