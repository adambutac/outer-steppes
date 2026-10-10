"""Discord OAuth2 login, used only to learn which Discord user is signing in.

DESIGN.md, Discord login for the connector: exchange the authorization code
with the application's client secret, read /users/@me once, and drop the
token. The token is never stored, logged, or revoked: revoking cancels the
member's whole authorization of the app, which can include their user
install (the PR 8 probe saw scope "identify applications.commands").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE = "https://discord.com/oauth2/authorize"
TIMEOUT_SECONDS = 10


class DiscordLoginError(Exception):
    """Discord did not complete the login (HTTP error, timeout, or a malformed answer)."""


@dataclass(frozen=True)
class DiscordUser:
    id: str
    username: str


class DiscordLogin:
    def __init__(self, session: aiohttp.ClientSession, application_id: str, client_secret: str,
                 redirect_uri: str, api_base: str = DISCORD_API) -> None:
        self._session = session
        self._application_id = application_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._api = api_base.rstrip("/")

    async def identify(self, code: str) -> DiscordUser:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        try:
            # No redirects: the token POST carries the client secret, and a 307/308
            # would resend it to wherever the redirect points.
            async with self._session.post(f"{self._api}/oauth2/token", timeout=timeout, allow_redirects=False, data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri,
                    "client_id": self._application_id, "client_secret": self._client_secret}) as resp:
                if resp.status != 200:
                    # Discord's error body names the problem, never a secret; the status is enough.
                    log.warning("discord login: token exchange returned HTTP %d", resp.status)
                    raise DiscordLoginError("token exchange failed")
                body = await resp.json(content_type=None)
            access_token = body.get("access_token") if isinstance(body, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise DiscordLoginError("no access token")
            async with self._session.get(f"{self._api}/users/@me", timeout=timeout, allow_redirects=False,
                                         headers={"Authorization": f"Bearer {access_token}"}) as resp:
                if resp.status != 200:
                    log.warning("discord login: /users/@me returned HTTP %d", resp.status)
                    raise DiscordLoginError("user lookup failed")
                me = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning("discord login failed: %s", type(exc).__name__)
            raise DiscordLoginError("discord unreachable") from None
        # The token goes out of scope here: never stored, never revoked.
        user_id = me.get("id") if isinstance(me, dict) else None
        username = me.get("username") if isinstance(me, dict) else None
        if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit():
            raise DiscordLoginError("malformed user")
        return DiscordUser(user_id, username if isinstance(username, str) else "")
