"""What the operator reads on /metrics after members use each command, through the real service."""

from __future__ import annotations

from ..conftest import command


async def test_every_command_a_member_runs_is_counted_under_its_own_name(boot):
    h = await boot()
    await h.register()
    await h.connect()  # /ask is refused without the connector (ADR-0017)

    await h.say(command("webhook", sub="status"))
    await h.say(command("status"))
    await h.say(command("settings"))
    await h.interact(command(prompt="hi"))
    await h.say(command("frobnicate"))  # an unknown name
    await h.say(command("leave", confirm=True))

    async with h.http.get(h.admin + "/metrics") as resp:
        text = await resp.text()
    for name, result in (("webhook", "ephemeral"), ("status", "ephemeral"), ("settings", "ephemeral"),
                         ("ask", "deferred"), ("leave", "ephemeral")):
        assert f'bridge_interactions_total{{command="{name}",result="{result}"}} 1' in text
    assert 'bridge_interactions_total{command="other",result="ephemeral"} 1' in text  # /frobnicate
    assert 'command="frobnicate"' not in text
