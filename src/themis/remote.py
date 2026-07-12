"""Controller-side engine adapter for the isolated agent service."""

from pathlib import Path

import httpx

from themis.engines.base import EngineError, EngineQuotaError, EngineUnavailableError


class RemoteEngine:
    def __init__(
        self, name: str, base_url: str, token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    def available(self) -> bool:
        # Credential presence is known only inside the isolated agent container.
        return True

    async def run(
        self, *, prompt: str, workspace: Path, model: str, effort: str,
        timeout: float, web_access: bool = False,
    ) -> str:
        payload = {
            "engine": self.name,
            "workspace": workspace.name,
            "prompt": prompt,
            "model": model,
            "effort": effort,
            "timeout": timeout,
            "web_access": web_access,
        }
        try:
            async with httpx.AsyncClient(
                timeout=timeout + 30, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/run",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as error:
            raise EngineError(f"agent service unavailable: {error}") from error
        try:
            data = response.json()
        except ValueError:
            detail = response.text.strip()[:500] or f"HTTP {response.status_code}"
            raise EngineError(f"agent returned a non-JSON response: {detail}") from None
        if not isinstance(data, dict):
            raise EngineError("agent returned an invalid JSON response")
        if response.is_success:
            return str(data.get("output", ""))
        detail = data.get("detail", "agent execution failed")
        code = detail.get("code") if isinstance(detail, dict) else None
        message = (
            str(detail.get("message", "agent execution failed"))
            if isinstance(detail, dict)
            else str(detail)
        )
        if response.status_code == 429:
            raise EngineQuotaError(message)
        if code == "engine_credentials_unavailable":
            raise EngineUnavailableError(message)
        raise EngineError(message)
