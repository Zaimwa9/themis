"""Controller-side engine adapter for the isolated agent service."""

from pathlib import Path

import httpx

from themis.engines.base import EngineError, EngineQuotaError, EngineUnavailableError


class RemoteEngine:
    def __init__(self, name: str, base_url: str, token: str) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._token = token

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
            async with httpx.AsyncClient(timeout=timeout + 30) as client:
                response = await client.post(
                    f"{self._base_url}/run",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as error:
            raise EngineError(f"agent service unavailable: {error}") from error
        data = response.json()
        if response.is_success:
            return str(data.get("output", ""))
        message = str(data.get("detail", "agent execution failed"))
        if response.status_code == 429:
            raise EngineQuotaError(message)
        if response.status_code == 503:
            raise EngineUnavailableError(message)
        raise EngineError(message)
