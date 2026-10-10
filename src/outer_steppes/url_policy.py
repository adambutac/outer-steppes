"""Automation URL policy (DESIGN.md, Automation URL policy).

The bridge owns the list of supported webhook providers. A registration URL
must match one provider's exact pattern in full, with no port, query,
fragment, userinfo, or alternate spelling, and every DNS answer for the
provider's host must be a public address. Runs at registration and again in
the worker before each POST (PR 4); the accepted addresses are returned so
the worker can pin its connection to them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass

DNS_TIMEOUT_SECONDS = 1.0

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

REJECTED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4",
    "::/128", "::1/128", "fe80::/10", "fc00::/7", "ff00::/8", "::ffff:0:0/96",
))

Resolver = Callable[[str], Awaitable[list[str]]]


@dataclass(frozen=True)
class Provider:
    """A supported webhook provider: one host and one exact URL shape."""

    name: str
    host: str
    pattern: re.Pattern[str]   # matched against the whole URL string
    canonical: str             # format string; {id} is the lowercased trigger id
    kind: str                  # the automation kind stored for this provider's triggers
    auth: str                  # "signature" (Standard Webhooks, whsec_) or "bearer" (Authorization header, crsr_)

    def match(self, url: str) -> str | None:
        """The canonical URL if `url` matches this provider exactly, else None."""
        found = self.pattern.fullmatch(url)
        return self.canonical.format(id=found["id"].lower()) if found else None


GROK = Provider(
    name="grok",
    host="grok.com",
    pattern=re.compile(rf"https://grok\.com/webhook/automation/(?P<id>{_UUID})"),
    canonical="https://grok.com/webhook/automation/{id}",
    kind="grok_webhook",
    auth="signature",
)

# Grok Bot routines: their webhooks are hosted on Cursor's automations service, with a bearer key (ADR-0020).
GROK_BOT = Provider(
    name="grok_bot",
    host="api2.cursor.sh",
    pattern=re.compile(rf"https://api2\.cursor\.sh/automations/webhook/(?P<id>{_UUID})"),
    canonical="https://api2.cursor.sh/automations/webhook/{id}",
    kind="grok_bot_webhook",
    auth="bearer",
)

PROVIDERS: tuple[Provider, ...] = (GROK, GROK_BOT)


class UrlRejected(Exception):
    """The automation URL fails policy. The message never contains the URL."""


@dataclass(frozen=True)
class AcceptedUrl:
    url: str          # canonical form; this is what is stored and POSTed to
    provider: str
    host: str
    addresses: tuple[str, ...]
    auth: str = "signature"


def match_provider(url: object, providers: Sequence[Provider] = PROVIDERS) -> tuple[Provider, str]:
    """Everything except DNS. Returns the provider and the canonical URL."""
    if isinstance(url, str):
        for provider in providers:
            canonical = provider.match(url)
            if canonical is not None:
                return provider, canonical
    raise UrlRejected("does not match a supported webhook provider")


def address_rejected(address: str, bind_hosts: Collection[str]) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if any(ip in network for network in REJECTED_NETWORKS if network.version == ip.version):
        return True
    return str(ip) in bind_hosts


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return sorted({info[4][0] for info in infos})


async def validate_automation_url(url: object, bind_hosts: Collection[str] = (),
                                  resolver: Resolver = system_resolver,
                                  providers: Sequence[Provider] = PROVIDERS) -> AcceptedUrl:
    provider, canonical = match_provider(url, providers)
    try:
        addresses = await asyncio.wait_for(resolver(provider.host), DNS_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError):
        raise UrlRejected("DNS lookup failed") from None
    if not addresses:
        raise UrlRejected("DNS returned no addresses")
    for address in addresses:
        try:
            rejected = address_rejected(address, bind_hosts)
        except ValueError:
            raise UrlRejected("DNS returned an invalid address") from None
        if rejected:
            raise UrlRejected("DNS answer is a private, loopback, or reserved address")
    return AcceptedUrl(canonical, provider.name, provider.host, tuple(addresses), provider.auth)
