"""The two outside parties of the onboarding journey, played by small clients (DESIGN.md, Testing).

They talk to the real listeners over real HTTP. Cookies are handled by hand because the account page's
`__Host-` cookie is `Secure` and the loopback server is plain HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from ..conftest import page_link

GROK_CALLBACK = "https://grok.com/connectors-oauth-exchange-code/"
ORIGIN = "https://bridge.example"  # PUBLIC_BASE_URL in the test config
PUBLIC_CLIENT_ID = "outer-steppes"


@dataclass
class Reply:
    status: int
    headers: object  # a case-insensitive multidict
    text: str

    def json(self):
        return json.loads(self.text)

    @property
    def location(self) -> str | None:
        return self.headers.get("Location")

    def set_cookies(self) -> list[str]:
        return list(self.headers.getall("Set-Cookie", []))

    def cookie(self, name: str) -> str | None:
        for header in self.set_cookies():
            parsed = SimpleCookie()
            parsed.load(header)
            if name in parsed:
                return parsed[name].value
        return None

    def query(self) -> dict[str, str]:
        """The query of the Location header, first value of each parameter."""
        return {k: v[0] for k, v in parse_qs(urlsplit(self.location or "").query).items()}

    def link(self, label: str) -> str:
        """The href of the page's link with this text, unescaped, as a browser would follow it."""
        return page_link(self.text, label)

    def link_query(self, label: str) -> dict[str, list[str]]:
        """Every value of each parameter in that link's query, so an added parameter shows."""
        return parse_qs(urlsplit(self.link(label)).query)


def s256(verifier: str) -> str:
    """PKCE S256, written out here so the test doesn't use the bridge's own function."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


@dataclass
class Tokens:
    access: str
    refresh: str
    body: dict


class Connector:
    """What grok.com does as the connector's client: authorize, return from Discord, exchange, refresh, call /mcp.

    By default it is the public client. Given an issued client's id and secret (`osctl connector client add`), it
    is that client instead: authorize answers with a code at once, and the token requests carry the secret by
    client_secret_post.
    """

    def __init__(self, harness, client_id: str = PUBLIC_CLIENT_ID, client_secret: str | None = None) -> None:
        self.h = harness
        self.client_id, self.client_secret = client_id, client_secret
        self.verifier = secrets.token_urlsafe(48)
        self.resource = harness.config.public_base_url + "/mcp"

    async def authorize(self) -> Reply:
        query = {"response_type": "code", "client_id": self.client_id, "redirect_uri": GROK_CALLBACK,
                 "state": "grok-state", "code_challenge": s256(self.verifier), "code_challenge_method": "S256",
                 "resource": self.resource, "scope": "mcp"}
        return await self.h.call("GET", "/oauth/authorize?" + "&".join(f"{k}={v}" for k, v in query.items()))

    async def sign_in(self, discord_user_id: str, username: str = "member") -> Tokens:
        """The whole connect: authorize, the member approves on Discord (played here), the code, the tokens."""
        code = await self.code_for(discord_user_id, username)
        return await self.exchange(code)

    async def exchange(self, code: str) -> Tokens:
        exchanged = await self.token({"grant_type": "authorization_code", "code": code, "redirect_uri": GROK_CALLBACK,
                                      "code_verifier": self.verifier, "resource": self.resource})
        assert exchanged.status == 200, exchanged.text
        body = exchanged.json()
        return Tokens(body["access_token"], body["refresh_token"], body)

    async def code_for(self, discord_user_id: str, username: str = "member") -> str:
        """Everything up to the bridge's own authorization code for grok.com, which a test may exchange or misuse."""
        # The public client stops on the connect-time warning; the member chooses Continue (DESIGN.md).
        started = await self.authorize()
        assert started.status == 200 and started.location is None, (started.status, started.text)
        assert started.link("Continue").startswith("https://discord.com/oauth2/authorize?")
        discord_state, nonce = started.link_query("Continue")["state"][0], started.cookie("__Host-os_login")
        code = self.h.discord.login_code(discord_user_id, username)
        returned = await self.h.call("GET", f"/oauth/discord/callback?state={discord_state}&code={code}",
                                     headers={"Cookie": f"__Host-os_login={nonce}"})
        assert returned.status == 302 and returned.location.startswith(GROK_CALLBACK), (returned.status, returned.text)
        assert returned.query()["state"] == "grok-state"
        return returned.query()["code"]

    async def issued_code(self) -> str:
        """An issued client's authorize: no Discord login, the code comes straight back to grok.com."""
        started = await self.authorize()
        assert started.status == 302 and started.location.startswith(GROK_CALLBACK), (started.status, started.text)
        assert started.query()["state"] == "grok-state"
        return started.query()["code"]

    async def token(self, form: dict) -> Reply:
        secret = {"client_secret": self.client_secret} if self.client_secret else {}
        return await self.h.call("POST", "/oauth/token", data={"client_id": self.client_id, **secret, **form})

    async def refresh(self, refresh_token: str) -> Reply:
        return await self.token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def mcp(self, access_token: str | None, method: str, params: dict | None = None, msg_id: int = 1) -> Reply:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        return await self.h.call("POST", "/mcp", headers=headers,
                                 json_body={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


class Browser:
    """A member's browser on the account page: it keeps the session cookie and sends it by hand."""

    def __init__(self, harness) -> None:
        self.h = harness
        self.session: str | None = None
        self.session_set_cookie: str | None = None  # the header as the bridge sent it, for the attribute checks

    def _cookie(self) -> dict:
        return {"Cookie": f"__Host-os_session={self.session}"} if self.session else {}

    async def sign_in(self, discord_user_id: str, username: str = "member") -> Reply:
        started = await self.h.call("GET", "/account/login")
        assert started.status == 302, started.text
        code = self.h.discord.login_code(discord_user_id, username)
        returned = await self.h.call(
            "GET", f"/oauth/discord/callback?state={started.query()['state']}&code={code}",
            headers={"Cookie": f"__Host-os_login={started.cookie('__Host-os_login')}"})
        assert returned.status == 303 and returned.location == ORIGIN + "/account", (returned.status, returned.text)
        self.session = returned.cookie("__Host-os_session")
        self.session_set_cookie = next(c for c in returned.set_cookies() if c.startswith("__Host-os_session="))
        return returned

    async def page(self, *, query: str = "") -> Reply:
        return await self.h.call("GET", "/account" + query, headers=self._cookie())

    async def csrf(self) -> str:
        """The token from the form the bridge gave this session, as a browser would submit it."""
        found = re.search(r"name=csrf value='([0-9a-f]{64})'", (await self.page()).text)
        assert found, "no CSRF token on the page"
        return found.group(1)

    async def post(self, path: str, data: dict | None = None, *, origin: str | None = ORIGIN, csrf: str | None = None,
                   with_session: bool = True) -> Reply:
        form = dict(data or {})
        form["csrf"] = csrf if csrf is not None else (await self.csrf() if self.session else "")
        headers = dict(self._cookie()) if with_session else {}
        if origin is not None:
            headers["Origin"] = origin
        return await self.h.call("POST", path, headers=headers, data=form)
