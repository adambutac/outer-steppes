"""Automation URL policy: exact provider formats, canonical URLs, public DNS."""

from __future__ import annotations

import asyncio
import re

import pytest

from outer_steppes.url_policy import (
    GROK, Provider, UrlRejected, address_rejected, match_provider, validate_automation_url,
)

TRIGGER_ID = "00000000-0000-4000-8000-000000000001"  # made up; not a real automation
GOOD = f"https://grok.com/webhook/automation/{TRIGGER_ID}"


def resolver_returning(*addresses):
    async def resolve(host):
        assert host == "grok.com"  # DNS is only ever asked about the provider's own host
        return list(addresses)
    return resolve


async def validate(url, resolver=resolver_returning("104.18.0.1"), bind_hosts=()):
    return await validate_automation_url(url, bind_hosts, resolver)


async def test_good_url_is_accepted_with_pinned_addresses():
    accepted = await validate(GOOD, resolver_returning("104.18.0.1", "2606:4700::6812:1"))
    assert accepted.url == GOOD
    assert (accepted.provider, accepted.host) == ("grok", "grok.com")
    assert accepted.addresses == ("104.18.0.1", "2606:4700::6812:1")


async def test_uppercase_trigger_id_is_canonicalized():
    accepted = await validate(GOOD.replace(TRIGGER_ID, TRIGGER_ID.upper().replace("0", "A")))
    assert accepted.url == "https://grok.com/webhook/automation/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


@pytest.mark.parametrize("url", [
    # Scheme, host, and port variations
    f"http://grok.com/webhook/automation/{TRIGGER_ID}",
    f"HTTPS://grok.com/webhook/automation/{TRIGGER_ID}",
    f"https://GROK.com/webhook/automation/{TRIGGER_ID}",
    f"https://www.grok.com/webhook/automation/{TRIGGER_ID}",
    f"https://grok.com./webhook/automation/{TRIGGER_ID}",
    f"https://grok.com:443/webhook/automation/{TRIGGER_ID}",
    f"https://grok.com:8443/webhook/automation/{TRIGGER_ID}",
    f"https://grok.com.evil.example/webhook/automation/{TRIGGER_ID}",
    f"https://evilgrok.com/webhook/automation/{TRIGGER_ID}",
    # Userinfo tricks that connect somewhere other than they appear to
    f"https://grok.com@127.0.0.1/webhook/automation/{TRIGGER_ID}",
    f"https://user:pass@grok.com/webhook/automation/{TRIGGER_ID}",
    # Localhost and internal targets, in several spellings
    f"https://127.0.0.1/webhook/automation/{TRIGGER_ID}",
    f"https://[::1]/webhook/automation/{TRIGGER_ID}",
    f"https://localhost/webhook/automation/{TRIGGER_ID}",
    f"https://2130706433/webhook/automation/{TRIGGER_ID}",
    f"https://0x7f.1/webhook/automation/{TRIGGER_ID}",
    f"https://169.254.169.254/webhook/automation/{TRIGGER_ID}",
    f"https://127.0.0.1.nip.io/webhook/automation/{TRIGGER_ID}",
    # Path variations
    f"https://grok.com/webhook/automation/{TRIGGER_ID}/",
    f"https://grok.com/webhook/automation/{TRIGGER_ID}/extra",
    f"https://grok.com/webhook/automation/../automation/{TRIGGER_ID}",
    f"https://grok.com/webhook/Automation/{TRIGGER_ID}",
    f"https://grok.com/webhook/automation/{TRIGGER_ID.replace('-', '%2D')}",
    f"https://grok.com//webhook/automation/{TRIGGER_ID}",
    "https://grok.com/webhook/automation/not-a-uuid",
    "https://grok.com/webhook/automation/00000000000040008000000000000001",
    "https://grok.com/webhook/automation/",
    # Query, fragment, whitespace, and control characters
    f"https://grok.com/webhook/automation/{TRIGGER_ID}?redirect=http://127.0.0.1",
    f"https://grok.com/webhook/automation/{TRIGGER_ID}#x",
    f" https://grok.com/webhook/automation/{TRIGGER_ID}",
    f"https://grok.com/webhook/automation/{TRIGGER_ID}\n",
    f"https://grok.com/webhook/automation/{TRIGGER_ID}\x00",
    # Not a string at all
    None, "", 42,
])
async def test_anything_but_the_exact_format_is_rejected(url):
    with pytest.raises(UrlRejected, match="does not match a supported webhook provider"):
        await validate(url)


@pytest.mark.parametrize("address", [
    "0.0.0.0", "10.1.2.3", "100.64.0.1", "127.0.0.1", "169.254.169.254",
    "172.16.0.1", "192.168.1.1", "224.0.0.1",
    "::", "::1", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:104.18.0.1",
])
async def test_grok_com_resolving_to_a_private_address_is_rejected(address):
    with pytest.raises(UrlRejected, match="private, loopback, or reserved"):
        await validate(GOOD, resolver_returning("104.18.0.1", address))


async def test_bind_address_is_rejected():
    with pytest.raises(UrlRejected):
        await validate(GOOD, resolver_returning("203.0.113.7"), bind_hosts={"203.0.113.7"})


@pytest.mark.parametrize("address", ["104.18.0.1", "2606:4700::6812:1", "203.0.113.7"])
def test_public_addresses_pass(address):
    assert not address_rejected(address, ())


async def test_dns_failure_and_empty_answer():
    async def fails(host):
        raise OSError("NXDOMAIN")
    with pytest.raises(UrlRejected, match="DNS lookup failed"):
        await validate(GOOD, fails)
    with pytest.raises(UrlRejected, match="no addresses"):
        await validate(GOOD, resolver_returning())


async def test_dns_is_capped(monkeypatch):
    from outer_steppes import url_policy
    monkeypatch.setattr(url_policy, "DNS_TIMEOUT_SECONDS", 0.05)

    async def hangs(host):
        await asyncio.sleep(10)
    with pytest.raises(UrlRejected, match="DNS lookup failed"):
        await validate(GOOD, hangs)


async def test_rejection_message_never_contains_the_url():
    with pytest.raises(UrlRejected) as excinfo:
        await validate("https://grok.com/webhook/automation/SUPERSECRETPATH")
    assert "SUPERSECRETPATH" not in str(excinfo.value)


def test_providers_are_owned_by_the_bridge():
    """Adding a provider is a code change: a name, one host, one exact pattern."""
    other = Provider("example", "hooks.example.test",
                     re.compile(r"https://hooks\.example\.test/t/(?P<id>[a-z0-9]{8})"),
                     "https://hooks.example.test/t/{id}", "grok_webhook", "signature")
    provider, canonical = match_provider("https://hooks.example.test/t/abcd1234", (GROK, other))
    assert (provider.name, canonical) == ("example", "https://hooks.example.test/t/abcd1234")
    with pytest.raises(UrlRejected):
        match_provider("https://hooks.example.test/t/abcd1234")  # not enabled by default


# ------------------------------------------------------------------ Grok Bot (ADR-0020)

@pytest.mark.parametrize("url,canonical", [
    ("https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0", "https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0"),
    ("https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000B0", "https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0"),
])
def test_a_grok_bot_webhook_url_is_its_own_provider(url, canonical):
    provider, stored = match_provider(url)
    assert (provider.name, provider.kind, provider.auth, stored) == ("grok_bot", "grok_bot_webhook", "bearer", canonical)


@pytest.mark.parametrize("url", [
    "http://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0",
    "https://API2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0",
    "https://api.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0",
    "https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0/",
    "https://api2.cursor.sh:443/automations/webhook/00000000-0000-4000-8000-0000000000b0",
    "https://api2.cursor.sh/automations/webhook/not-a-uuid",
    "https://grok.com/automations/webhook/00000000-0000-4000-8000-0000000000b0",
])
def test_grok_bot_url_variations_are_refused(url):
    with pytest.raises(UrlRejected):
        match_provider(url)


async def test_a_grok_bot_url_is_accepted_for_a_bearer_key():
    """The worker picks the bearer header from AcceptedUrl.auth, which comes from the provider (ADR-0020)."""
    async def resolver(host):
        assert host == "api2.cursor.sh"
        return ["104.18.0.1"]

    accepted = await validate_automation_url("https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0", (), resolver)
    assert (accepted.provider, accepted.host, accepted.auth) == ("grok_bot", "api2.cursor.sh", "bearer")
    grok = await validate_automation_url("https://grok.com/webhook/automation/00000000-0000-4000-8000-000000000001",
                                         (), resolver=lambda host: _answer(["104.18.0.2"]))
    assert grok.auth == "signature"


async def _answer(addresses):
    return addresses
