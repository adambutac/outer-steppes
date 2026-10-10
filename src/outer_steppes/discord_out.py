"""Discord writes made with the interaction token only (never a bot token).

The request URL contains the interaction token, so it is never logged.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass

import aiohttp

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://discord.com/api/v10"
USER_AGENT = f"DiscordBot (outer-steppes, {__version__})"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RATE_LIMIT_WAIT_SECONDS = 5.0

NO_MENTIONS = {"parse": []}


class Outcome(enum.Enum):
    OK = "ok"
    NOT_FOUND = "not_found"        # 404: token gone, or deferral not processed yet
    UNAUTHORIZED = "unauthorized"  # 401: token invalid or expired
    FAILED = "failed"              # network error, 5xx, other 4xx, or 429 after one retry


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    status: int | None = None


class DiscordClient:
    def __init__(self, session: aiohttp.ClientSession, application_id: str,
                 api_base: str = DEFAULT_API_BASE) -> None:
        self._session = session
        self._application_id = application_id
        self._api_base = api_base.rstrip("/")

    async def edit_original(self, interaction_token: str, content: str) -> Result:
        """PATCH the deferred response. Mentions never ping."""
        url = f"{self._api_base}/webhooks/{self._application_id}/{interaction_token}/messages/@original"
        body = {"content": content, "allowed_mentions": NO_MENTIONS}
        return await self._send("PATCH", url, body)

    async def create_followup(self, interaction_token: str, content: str, ephemeral: bool) -> Result:
        """POST a followup (answer chunks 2+). Followups set their own flags."""
        url = f"{self._api_base}/webhooks/{self._application_id}/{interaction_token}"
        body: dict = {"content": content, "allowed_mentions": NO_MENTIONS}
        if ephemeral:
            body["flags"] = 64
        return await self._send("POST", url, body)

    async def _send(self, method: str, url: str, body: dict) -> Result:
        for attempt in range(2):
            try:
                async with self._session.request(
                    method, url, json=body,
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                ) as resp:
                    if 200 <= resp.status < 300:
                        return Result(Outcome.OK, resp.status)
                    if resp.status == 404:
                        return Result(Outcome.NOT_FOUND, resp.status)
                    if resp.status == 401:
                        return Result(Outcome.UNAUTHORIZED, resp.status)
                    if resp.status == 429 and attempt == 0:
                        wait = await _retry_after(resp)
                        if wait is not None and wait <= MAX_RATE_LIMIT_WAIT_SECONDS:
                            await asyncio.sleep(wait)
                            continue
                    log.warning("discord %s failed with HTTP %s", method, resp.status)
                    return Result(Outcome.FAILED, resp.status)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("discord %s error: %s", method, type(exc).__name__)
                return Result(Outcome.FAILED)
        return Result(Outcome.FAILED, 429)


async def _retry_after(resp: aiohttp.ClientResponse) -> float | None:
    header = resp.headers.get("Retry-After")
    try:
        if header is not None:
            return float(header)
        data = await resp.json(content_type=None)
        return float(data.get("retry_after"))
    except (TypeError, ValueError, AttributeError, aiohttp.ContentTypeError):
        return None
