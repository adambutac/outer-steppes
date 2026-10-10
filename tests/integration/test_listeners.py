"""What the two real listeners accept and refuse."""

from __future__ import annotations

import time

import pytest

from ..conftest import command


async def test_an_unsigned_or_stale_interaction_is_refused_and_starts_nothing(boot):
    h = await boot()
    await h.register()

    status, body = await h.interact(command(), sign=False)
    assert status == 401 and body == {"text": "invalid request signature"}
    status, _ = await h.interact(command(), timestamp=int(time.time()) - 3600)  # validly signed, an hour old
    assert status == 401

    assert h.automation.posts == [] and h.discord.calls == []
    assert h.db().execute("SELECT count(*) FROM interactions").fetchone()[0] == 0  # not even a dedup claim


@pytest.mark.parametrize("path", ["/v1/registrations", "/v1/status", "/metrics", "/readyz"])
async def test_the_admin_routes_are_not_on_the_public_listener(boot, path):
    h = await boot()
    async with h.http.get(h.public + path) as resp:
        assert resp.status == 404


async def test_the_admin_listener_wants_the_bearer_but_metrics_and_readyz_do_not(boot):
    h = await boot()
    status, _ = await h.operator("GET", "/v1/registrations", bearer=False)
    assert status == 401
    for path in ("/metrics", "/readyz"):
        async with h.http.get(h.admin + path) as resp:
            assert resp.status == 200
