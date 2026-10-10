"""The grok.com connector's OAuth past the first connect, on the real service: the issued-client fallback, refresh
rotation, the connection cap, tokens used as the wrong kind, /mcp's bearer check, and disconnecting (DESIGN.md, OAuth
for the grok.com connector, and MCP server).

The first connect itself (authorize with PKCE, the Discord callback, the code exchange) is in test_onboarding.py.
"""

from __future__ import annotations

import base64
import time
from contextlib import closing

from outer_steppes.oauth import MSG_CONNECTION_INVALID, MSG_UNKNOWN_CLIENT

from .clients import Connector
from .conftest import eventually

USER = "555555555555555555"
INVALID_GRANT = {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID}
INVALID_TOKEN = f'Bearer realm="outer-steppes", error="invalid_token", error_description="{MSG_CONNECTION_INVALID}"'


async def connections(h) -> list[dict]:
    """What `osctl connector list` shows for the member."""
    status, body = await h.operator("GET", f"/v1/registrations/{USER}/mcp-connections")
    assert status == 200, body
    return body


def access_rows(h) -> int:
    with closing(h.db()) as conn:
        return conn.execute("SELECT count(*) FROM oauth_tokens WHERE kind = 'access'").fetchone()[0]


async def works(connector: Connector, tokens) -> bool:
    """Whether the connection still answers: its access token reaches /mcp."""
    return (await connector.mcp(tokens.access, "tools/list")).status == 200


async def assert_ended(connector: Connector, tokens) -> None:
    """A connection the bridge no longer knows: /mcp and the refresh both say to reconnect."""
    dead = await connector.mcp(tokens.access, "tools/list")
    assert (dead.status, dead.headers["WWW-Authenticate"]) == (401, INVALID_TOKEN)
    refreshed = await connector.refresh(tokens.refresh)
    assert (refreshed.status, refreshed.json()) == (400, INVALID_GRANT)


async def test_an_issued_client_connects_without_discord_and_deleting_it_ends_its_connection(boot):
    h = await boot()  # no Discord login: the fallback is for when there is none
    await h.register(USER)
    status, issued = await h.operator("POST", f"/v1/registrations/{USER}/mcp-clients")
    assert status == 200, issued
    assert issued["client_id"].startswith("osc_") and issued["client_secret"].startswith("oss_")
    assert issued["token_auth_method"] == "client_secret_post"
    connector = Connector(h, issued["client_id"], issued["client_secret"])

    # Holding the secret is the authorization: authorize answers with a code at once, straight back to grok.com.
    tokens = await connector.exchange(await connector.issued_code())
    assert await works(connector, tokens)
    assert [c["client_kind"] for c in await connections(h)] == ["issued"]

    # The token endpoint wants the client's secret: none, a wrong one, or both post and Basic are refused.
    basic = base64.b64encode(f"{issued['client_id']}:{issued['client_secret']}".encode()).decode()
    refused = {
        "no secret": await Connector(h, issued["client_id"]).refresh(tokens.refresh),
        "wrong secret": await Connector(h, issued["client_id"], "oss_made-up").refresh(tokens.refresh),
        "post and basic": await h.call("POST", "/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
            "client_id": issued["client_id"], "client_secret": issued["client_secret"],
            "grant_type": "refresh_token", "refresh_token": tokens.refresh}),
    }
    for why, reply in refused.items():
        assert (reply.status, reply.json()) == (401, {"error": "invalid_client"}), why
        assert reply.headers["WWW-Authenticate"] == 'Basic realm="outer-steppes"', why

    # A refresh token belongs to the client it was issued to: another client of the same member can't use it.
    status, other = await h.operator("POST", f"/v1/registrations/{USER}/mcp-clients")
    assert status == 200, other
    borrowed = await Connector(h, other["client_id"], other["client_secret"]).refresh(tokens.refresh)
    assert (borrowed.status, borrowed.json()) == (400, INVALID_GRANT)

    # client_secret_basic works as well as client_secret_post. (The refusals above didn't spend the refresh token.)
    rotated = await h.call("POST", "/oauth/token", headers={"Authorization": f"Basic {basic}"},
                           data={"grant_type": "refresh_token", "refresh_token": tokens.refresh})
    assert rotated.status == 200, rotated.text
    tokens.access, tokens.refresh = rotated.json()["access_token"], rotated.json()["refresh_token"]
    assert await works(connector, tokens)

    # `osctl connector client delete` revokes the client, and its connection goes with it.
    status, body = await h.operator("DELETE", f"/v1/mcp-clients/{issued['client_id']}")
    assert (status, body) == (200, {"deleted": True})
    dead = await connector.mcp(tokens.access, "tools/list")
    assert (dead.status, dead.headers["WWW-Authenticate"]) == (401, INVALID_TOKEN)
    gone = await connector.refresh(tokens.refresh)
    assert (gone.status, gone.json()) == (401, {"error": "invalid_client"})  # the client itself is unknown now
    again = await connector.authorize()
    assert again.status == 400 and MSG_UNKNOWN_CLIENT in again.text and again.location is None  # never redirects
    assert await connections(h) == []
    assert await h.members() == [USER]  # the member stays


async def test_a_refresh_rotates_the_tokens_and_keeps_the_connection(boot):
    h = await boot(login=True, claimed_by=USER, sweep_interval=0.05)
    connector = Connector(h)
    first = await connector.sign_in(USER)
    # As grok.com does it: connected an hour ago, the access token has expired and been swept, and now it refreshes.
    # Only the refresh token's row is left to carry when the connection was made, so a reset would show.
    h.write("UPDATE oauth_tokens SET connected_at = connected_at - 3600")
    h.write("UPDATE oauth_tokens SET expires_at = ? WHERE kind = 'access'", (int(time.time()) - 1,))
    await eventually(lambda: access_rows(h) == 0, "the sweeper to delete the expired access token")
    [before] = await connections(h)

    refreshed = await connector.refresh(first.refresh)
    assert refreshed.status == 200, refreshed.text
    assert refreshed.headers["Cache-Control"] == "no-store"
    body = refreshed.json()
    assert (body["token_type"], body["scope"]) == ("Bearer", "mcp")
    assert body["access_token"].startswith("mcp_") and body["refresh_token"].startswith("mcpr_")
    assert {body["access_token"], body["refresh_token"]}.isdisjoint({first.access, first.refresh})
    second = type(first)(body["access_token"], body["refresh_token"], body)
    assert await works(connector, second)

    # The same connection, with the time it was first connected.
    [after] = await connections(h)
    assert (after["connection_id"], after["created_at"]) == (before["connection_id"], before["created_at"])

    # Each refresh token works once; the new one goes on working.
    reused = await connector.refresh(first.refresh)
    assert (reused.status, reused.json()) == (400, INVALID_GRANT)
    assert (await connector.refresh(second.refresh)).status == 200
    assert [c["connection_id"] for c in await connections(h)] == [before["connection_id"]]


async def test_a_sixth_connection_evicts_the_earliest_connected_one(boot):
    h = await boot(login=True, claimed_by=USER)
    connectors = [Connector(h) for _ in range(6)]
    oldest = await connectors[0].sign_in(USER)
    h.write("UPDATE oauth_tokens SET connected_at = connected_at - 3600")  # the only one connected an hour ago
    # Refreshing it is use, but it doesn't make it newer: eviction goes by when a connection was first made.
    refreshed = (await connectors[0].refresh(oldest.refresh)).json()
    oldest.access, oldest.refresh = refreshed["access_token"], refreshed["refresh_token"]
    middle = [await c.sign_in(USER) for c in connectors[1:5]]
    # The other four connected and were last used a minute ago, so the oldest is now strictly the most recently used:
    # an eviction by use instead of by first connection would pick one of them, every time.
    h.write("UPDATE oauth_tokens SET connected_at = connected_at - 60, created_at = created_at - 60, "
            "last_used_at = last_used_at - 60 WHERE connected_at > ?", (int(time.time()) - 1800,))
    assert len(await connections(h)) == 5
    for connector, tokens in zip(connectors, [oldest, *middle]):
        assert await works(connector, tokens)

    newest = await connectors[5].sign_in(USER)
    assert len(await connections(h)) == 5
    await assert_ended(connectors[0], oldest)
    for connector, tokens in zip(connectors[1:], [*middle, newest]):
        assert await works(connector, tokens)


async def test_an_access_token_is_no_refresh_token_and_a_refresh_token_is_no_access_token(boot):
    h = await boot(login=True, claimed_by=USER)
    connector = Connector(h)
    tokens = await connector.sign_in(USER)

    as_refresh = await connector.refresh(tokens.access)
    assert (as_refresh.status, as_refresh.json()) == (400, INVALID_GRANT)
    as_bearer = await connector.mcp(tokens.refresh, "tools/list")
    assert (as_bearer.status, as_bearer.headers["WWW-Authenticate"]) == (401, INVALID_TOKEN)

    # Neither misuse spent anything: each token still works as its own kind.
    assert await works(connector, tokens)
    assert (await connector.refresh(tokens.refresh)).status == 200


async def test_mcp_needs_a_live_access_token_and_counts_every_refusal(boot):
    h = await boot(login=True, claimed_by=USER)
    connector = Connector(h)
    tokens = await connector.sign_in(USER)

    # No header at all: the bare challenge, with no error (RFC 6750 3.1).
    bare = await connector.mcp(None, "tools/list")
    assert (bare.status, bare.headers["WWW-Authenticate"]) == (401, 'Bearer realm="outer-steppes"')

    # A header the bridge can't accept: the challenge says the connection is invalid, and to reconnect.
    refused = {
        "made-up token": await connector.mcp("mcp_made-up", "tools/list"),
        "not Bearer": await h.call("POST", "/mcp", headers={"Authorization": f"Basic {tokens.access}"},
                                   json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    }
    for why, reply in refused.items():
        assert (reply.status, reply.headers["WWW-Authenticate"]) == (401, INVALID_TOKEN), why

    ok = await connector.mcp(tokens.access, "initialize", {"protocolVersion": "2025-06-18"})
    assert ok.status == 200 and ok.json()["result"]["protocolVersion"] == "2025-06-18"

    h.write("UPDATE oauth_tokens SET expires_at = ? WHERE kind = 'access'", (int(time.time()) - 1,))
    expired = await connector.mcp(tokens.access, "tools/list")
    assert (expired.status, expired.headers["WWW-Authenticate"]) == (401, INVALID_TOKEN)
    assert (await connector.refresh(tokens.refresh)).status == 200  # what grok.com does next, and it works

    async with h.http.get(h.admin + "/metrics") as resp:
        assert "\nbridge_mcp_auth_failures_total 4\n" in await resp.text()


async def test_the_operator_disconnects_one_connection_or_all_of_them(boot):
    h = await boot(login=True, claimed_by=USER)
    first, second, third = Connector(h), Connector(h), Connector(h)
    first_tokens = await first.sign_in(USER)
    h.write("UPDATE oauth_tokens SET connected_at = connected_at - 3600")  # so it lists first
    second_tokens = await second.sign_in(USER)
    listed = await connections(h)
    assert [c["client_kind"] for c in listed] == ["public", "public"]

    # `osctl connector disconnect <id> <connection>`: that one ends, the other is untouched.
    path = f"/v1/registrations/{USER}/mcp-connections"
    assert await h.operator("DELETE", f"{path}/{listed[0]['connection_id']}") == (200, {"disconnected": 1})
    await assert_ended(first, first_tokens)
    assert await works(second, second_tokens)
    assert [c["connection_id"] for c in await connections(h)] == [listed[1]["connection_id"]]
    status, _ = await h.operator("DELETE", f"{path}/{listed[0]['connection_id']}")
    assert status == 404  # already gone

    # `osctl connector disconnect <id>` with no connection: all of them end, and the member stays.
    third_tokens = await third.sign_in(USER)
    assert await h.operator("DELETE", path) == (200, {"disconnected": 2})
    await assert_ended(second, second_tokens)
    await assert_ended(third, third_tokens)
    assert await connections(h) == []
    assert await h.members() == [USER]
