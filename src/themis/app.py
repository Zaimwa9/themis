"""App factory: fail-fast settings, identity resolution, queue lifecycle,
optional webhook self-registration."""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from themis.config import Settings, load_settings
from themis.github.auth import get_app_slug, make_app_jwt, update_webhook_url
from themis.queue import InMemoryJobQueue
from themis.router import create_router

logger = logging.getLogger(__name__)


async def _discover_tunnel_url(tunnel_api: str, attempts: int = 30) -> str | None:
    """Public https URL from the ngrok agent API (GET /api/tunnels), or None.

    Polls because the tunnel sidecar may still be establishing its session
    when Themis starts.
    """
    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(attempts):
            try:
                response = await client.get(f"{tunnel_api}/api/tunnels")
                response.raise_for_status()
                for tunnel in response.json().get("tunnels", []):
                    url = tunnel.get("public_url", "")
                    if url.startswith("https://"):
                        return url.rstrip("/")
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    return None


async def _register_webhook(settings: Settings, app_jwt: str) -> None:
    """Point the GitHub App's webhook at this instance, when configured.

    THEMIS_PUBLIC_URL wins over tunnel discovery. Failures warn and keep
    serving: the operator may have configured the webhook manually.
    """
    url = settings.public_url
    if url is None and settings.tunnel_api:
        url = await _discover_tunnel_url(settings.tunnel_api)
        if url is None:
            logger.warning("themis_tunnel_discovery_failed api=%s", settings.tunnel_api)
            return
    if url is None:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await update_webhook_url(client, f"{url}/webhook", app_jwt)
        logger.info("themis_webhook_registered url=%s/webhook", url)
    except httpx.HTTPError as error:
        logger.warning("themis_webhook_registration_failed error=%s", error)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()  # SettingsError here = crash, on purpose
    queue = InMemoryJobQueue(concurrency=settings.concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Resolving the slug also proves the App credentials work; a bad key
        # or client id fails the boot loudly instead of 500ing per-webhook.
        app_jwt = make_app_jwt(settings.gh_app_client_id, settings.gh_app_private_key_pem)
        async with httpx.AsyncClient(timeout=30) as client:
            slug = await get_app_slug(client, app_jwt)
        app.state.bot_slug = slug
        logger.info(
            "themis_started slug=%s mention=@%s engine=%s webhook_enabled=%s api_enabled=%s",
            slug, slug, settings.engine, settings.webhook_enabled, bool(settings.api_token),
        )
        await _register_webhook(settings, app_jwt)
        queue.start()
        yield
        await queue.stop()

    app = FastAPI(title="themis", lifespan=lifespan)
    app.include_router(create_router(settings, queue))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
