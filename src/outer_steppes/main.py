"""Process entry point.

Order matters (DESIGN.md, Idempotency): the database is checked and
startup reconciliation runs before either listener opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp
from aiohttp import web

from . import db, metrics
from .automation_client import AutomationClient
from .config import Config, ConfigError, load_config
from .discord_login import DiscordLogin
from .discord_out import DEFAULT_API_BASE, DiscordClient
from .http_app import create_public_app
from .interactions import InteractionHandler
from .loopback_api import create_admin_app
from .mcp_server import McpServer
from .oauth import OAuthServer
from .reply import HeldPrompts, ReplyService
from .web_account import AccountPages
from .sweeper import SWEEP_INTERVAL_SECONDS, Finalizer, reconcile_startup, run_sweeper
from .url_policy import Resolver, system_resolver
from .worker import Poster, WorkerPool

log = logging.getLogger("outer_steppes")


@dataclass(frozen=True)
class Stack:
    """What `running` started: the ports actually bound (a configured port 0 is resolved here), and the held
    prompts, so integration tests can see that ended asks don't keep theirs."""

    public_port: int
    admin_port: int
    held: HeldPrompts


@contextlib.asynccontextmanager
async def running(config: Config, *, discord_api_base: str = DEFAULT_API_BASE, poster: Poster | None = None,
                  resolver: Resolver = system_resolver,
                  sweep_interval: float = SWEEP_INTERVAL_SECONDS) -> AsyncIterator[Stack]:
    """Everything the service runs, started in order and stopped on exit (DESIGN.md, Testing).

    `serve` is this plus waiting forever. The keyword arguments are the existing injection
    points the integration tests use to put fakes outside the system: Discord's API base,
    the poster that sends to an automation, and the DNS resolver. The defaults are production:
    `serve` passes none of them, and no config value or environment variable reaches them, so the
    URL policy's DNS check and the real client can't be swapped out from outside the process.
    """
    db.initialize(config.database_path)
    db.check_file_modes(config.database_path)
    ack_conn = db.connect(config.database_path, db.ACK_BUSY_TIMEOUT_MS)
    bg_conn = db.connect(config.database_path, db.BACKGROUND_BUSY_TIMEOUT_MS)
    admin_conn = db.connect(config.database_path, db.BACKGROUND_BUSY_TIMEOUT_MS)

    counts = reconcile_startup(bg_conn, now=time.time())
    log.info("startup reconciliation: %s", counts)

    runners: list[web.AppRunner] = []
    background: list[asyncio.Task] = []
    async with aiohttp.ClientSession() as session:
        discord = DiscordClient(session, config.discord_application_id, discord_api_base)
        # Prompts live only here, in memory (ADR-0017). The finalizer drops those of ended interactions.
        held = HeldPrompts()
        finalizer = Finalizer(bg_conn, discord, on_pass=lambda: held.drop_ended(bg_conn))
        bind_hosts = {config.public_bind.host, config.admin_bind.host}
        pool = WorkerPool(bg_conn, finalizer, config, poster or AutomationClient(config.automation_timeout_seconds),
                          resolver=resolver, bind_hosts=bind_hosts)
        replies = ReplyService(bg_conn, discord, config, finalizer.wake, held)

        def spawn(interaction_id: str, prompt: str) -> None:
            held.hold(interaction_id, prompt)  # before the worker can mark the row posted (T3)
            pool.spawn(interaction_id)
        oauth_server = OAuthServer(bg_conn, config)
        discord_login = (DiscordLogin(session, config.discord_application_id, config.discord_client_secret,
                                      oauth_server.discord_redirect_uri, api_base=discord_api_base)
                         if config.discord_login_enabled else None)
        handler = InteractionHandler(config, ack_conn, posts_in_flight=lambda: pool.posts_in_flight)
        metrics.METRICS.inflight.read = lambda: pool.posts_in_flight
        try:
            ports: list[int] = []
            for app, bind in (
                (create_public_app(config, handler, spawn, finalizer.wake,
                                   oauth=oauth_server, mcp=McpServer(oauth_server, replies),
                                   discord_login=discord_login,
                                   account=AccountPages(bg_conn, config, finalizer.wake, resolver=resolver,
                                                        bind_hosts=bind_hosts)),
                 config.public_bind),
                (create_admin_app(config, admin_conn, finalizer.wake, resolver=resolver, bind_hosts=bind_hosts,
                                  background_alive=lambda: bool(background) and not any(t.done() for t in background)),
                 config.admin_bind),
            ):
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                runners.append(runner)
                await web.TCPSite(runner, bind.host, bind.port).start()
                ports.append(runner.addresses[0][1])
            log.info("listening: public %s:%d, admin %s:%d",
                     config.public_bind.host, ports[0], config.admin_bind.host, ports[1])

            background.append(asyncio.create_task(finalizer.run_forever(), name="finalizer"))
            background.append(asyncio.create_task(
                run_sweeper(bg_conn, finalizer, pool.live_task_ids, config.queue_wait_seconds,
                            member_idle_days=config.member_idle_days, interval=sweep_interval),
                name="sweeper"))
            finalizer.wake()  # send any final messages left over from before the restart
            yield Stack(public_port=ports[0], admin_port=ports[1], held=held)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await pool.close()
            for runner in runners:
                await runner.cleanup()
            ack_conn.close()
            bg_conn.close()
            admin_conn.close()


async def serve(config: Config) -> None:
    async with running(config):
        await asyncio.Event().wait()


class JsonFormatter(logging.Formatter):
    """One JSON object per line (DESIGN.md, Observability).

    Only the message and a fixed set of fields are emitted. Log calls never
    pass secrets, tokens, URLs, or prompt text in the first place.
    """

    FIELDS = ("interaction_id", "correlation_id")

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for field in self.FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def main() -> int:
    os.umask(0o077)
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(f"outer-steppes: {exc}", file=sys.stderr)
        return 2

    configure_logging()
    try:
        asyncio.run(serve(config))
    except db.DatabaseError as exc:
        print(f"outer-steppes: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
