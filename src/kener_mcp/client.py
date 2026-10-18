"""Thin async client for the Kener v4 REST API (tested against Kener 4.1.7)."""

from typing import Any, Literal

import httpx

from . import __version__

IncidentState = Literal["INVESTIGATING", "IDENTIFIED", "MONITORING", "RESOLVED"]
Impact = Literal["DOWN", "DEGRADED"]

DEFAULT_TIMEOUT = 20.0


class KenerError(Exception):
    """Kener returned an error, or could not be reached."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KenerClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v4",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": f"kener-mcp/{__version__}",
            },
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "KenerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            # The message names the URL only. Headers (and the key) stay out of it.
            raise KenerError(f"Request to {e.request.url} failed: {type(e).__name__}") from None
        if resp.is_success:
            return resp.json() if resp.content else {}
        message = resp.reason_phrase
        try:
            message = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise KenerError(f"Kener returned {resp.status_code}: {message}", resp.status_code)

    async def list_monitors(self) -> list[dict]:
        return (await self._request("GET", "/monitors"))["monitors"]

    async def list_incidents(self) -> list[dict]:
        return (await self._request("GET", "/incidents"))["incidents"]

    async def get_incident(self, incident_id: int) -> dict:
        return (await self._request("GET", f"/incidents/{incident_id}"))["incident"]

    async def list_comments(self, incident_id: int) -> list[dict]:
        return (await self._request("GET", f"/incidents/{incident_id}/comments"))["comments"]

    async def add_comment(
        self,
        incident_id: int,
        comment: str,
        state: IncidentState,
        timestamp: int | None = None,
    ) -> dict:
        # Kener 4.1.7 reads "comment" and "state". Its docs show "message"/"status",
        # which the server rejects.
        body: dict[str, Any] = {"comment": comment, "state": state}
        if timestamp is not None:
            body["timestamp"] = timestamp
        return (await self._request("POST", f"/incidents/{incident_id}/comments", json=body))[
            "comment"
        ]

    async def create_incident(
        self,
        title: str,
        start_date_time: int,
        monitors: list[dict] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"title": title, "start_date_time": start_date_time}
        if monitors:
            body["monitors"] = monitors
        return (await self._request("POST", "/incidents", json=body))["incident"]

    async def delete_incident(self, incident_id: int) -> dict:
        return await self._request("DELETE", f"/incidents/{incident_id}")
