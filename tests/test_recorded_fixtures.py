"""Recorded real-client traffic holds only made-up values (DESIGN.md, Testing, "Recorded traffic").

The fixtures under tests/integration/fixtures/grok_connector/ are scrubbed copies of
real access logs. These checks fail if a re-recording leaves a real address,
host, credential or signature in place.
"""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

FIXTURES = Path(__file__).parent / "integration" / "fixtures" / "grok_connector"
FAKE_HOST = "bridge.example.test"
# Hosts a recording may name: the made-up bridge and the two real services whose public URLs are fixed.
ALLOWED_URL_HOSTS = {FAKE_HOST, "grok.com", "discord.com"}
RFC7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
FIRST_TS = 1_800_000_000
# Documentation ranges, one per party, so a recording keeps who sent what (README, "How it was scrubbed").
SOURCE_NETS = {
    "discord": ipaddress.ip_network("192.0.2.0/24"),
    "grok": ipaddress.ip_network("198.51.100.0/24"),
    "browser": ipaddress.ip_network("203.0.113.0/24"),
}
DOC_NETS = [*SOURCE_NETS.values(), ipaddress.ip_network("2001:db8::/32")]
ALWAYS_REDACTED_HEADERS = ("Authorization", "Cookie")
ALWAYS_REDACTED_QUERY = ("code", "state")
# A Chromium reduced User-Agent version ("Chrome/154.0.0.0"): only a product name, not after a
# "/", followed by a major version and ".0.0.0". Anything else shaped like an address is scanned.
VERSION_TOKEN = re.compile(r"(?<![/\w.-])[A-Za-z][\w.-]*/\d+\.0\.0\.0(?![\w.])")
# The only client id a recording may carry: the default public client (config.py), not a
# deployment's own `osc_…` client from `osctl mcp-client`.
PUBLIC_CLIENT_ID = "outer-steppes"
# The scrub derives X-Signature-Timestamp from the shifted `ts`; allow for Discord's signing
# clock and the request's handling time differing by a few seconds in a re-recording.
SIGNATURE_TS_TOLERANCE = 5
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
# Candidate IPv6 literals: hex groups with at least two colons; ipaddress decides.
IPV6 = re.compile(r"(?<![\w:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")
URL_HOST = re.compile(r"https?://(\[[^\]]*\]|[^/\s\"'()\]\[?#%]+)")
RECORDING_SUFFIXES = {".jsonl"}


def _files():
    # Recursive: a recording filed in a subfolder is checked too.
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file() and p != FIXTURES / "README.md")


def _texts(path):
    """The file's text, then decoded again and again until stable, so an encoded value is scanned too."""
    texts = [path.read_text(encoding="utf-8")]
    while (decoded := unquote(texts[-1])) != texts[-1]:
        texts.append(decoded)
    return texts


def _disallowed_url_hosts(text):
    return [h for h in URL_HOST.findall(text) if h.lower() not in ALLOWED_URL_HOSTS]


def _records(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _allowed_ip(text):
    ip = ipaddress.ip_address(text)
    return ip.is_loopback or any(ip.version == net.version and ip in net for net in DOC_NETS)


def _disallowed_ip_literals(text):
    """Every IP address in free text that is neither loopback nor a documentation address."""
    found = IPV4.findall(VERSION_TOKEN.sub(" ", text))
    for candidate in IPV6.findall(text):
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
        found.append(candidate)
    return [ip for ip in found if not _allowed_ip(ip)]


def _source(user_agent):
    if user_agent.startswith("Discord-Interactions"):
        return "discord"
    if user_agent.startswith(("grok-", "Grok")):
        return "grok"
    return "browser"


def test_there_is_at_least_one_recording():
    assert _files(), f"no recordings under {FIXTURES}"


def test_every_file_is_a_format_this_guard_checks():
    # A recording in a new format (a HAR from a body capture, say) needs checks written for it first.
    for path in _files():
        assert path.suffix in RECORDING_SUFFIXES, f"{path.name}: no hygiene checks for this format"


@pytest.mark.parametrize("text", [
    "https://owner-home.example.net/",
    "redirect_uri=https%3A%2F%2Fother.host%2Fcb",  # percent-encoded, as many OAuth clients send it
    "https://[2606:4700::1111]/",
])
def test_the_host_scan_flags_other_hosts(text):
    assert _disallowed_url_hosts(text) or _disallowed_url_hosts(unquote(text))


@pytest.mark.parametrize("encoded", ["next=%2F10.0.0.7%2F", "next=%252F10.0.0.7%252F"])
def test_the_scans_see_percent_encoded_values(tmp_path, encoded):
    path = tmp_path / "r.jsonl"
    path.write_text(encoded, encoding="utf-8")
    assert any(_disallowed_ip_literals(text) for text in _texts(path))


@pytest.mark.parametrize("text", [
    "https://8.8.8.8/x",                      # an IP-literal URL
    '"uri":"/mcp/10.0.0.7"',                  # a path segment after a word
    "next=/foo/10.0.0.7",
    '"Referer":["http://35.196.132.85/"]',
    '"user_id":"10.0.0.7"',
    '"Origin":["https://[2606:4700::1111]/"]',  # IPv6, bracketed in a URL
    '"x":"2a00:1450:4001::200e"',
    "?next=a/10.0.0.7",                        # word/address that isn't a ".0.0.0" version
    '"x":"host 35.196.132.85"',
])
def test_the_scan_flags_real_addresses_anywhere(text):
    assert _disallowed_ip_literals(text)


@pytest.mark.parametrize("text", [
    "Mozilla/5.0 (Windows NT 10.0) Chrome/154.0.0.0 Safari/537.36",
    '"remote_ip":"::1"',
    "https://192.0.2.1/ and 2001:db8::5",
    '"Mcp-Protocol-Version":["2025-06-18"]',
])
def test_the_scan_allows_versions_loopback_and_documentation_addresses(text):
    assert _disallowed_ip_literals(text) == []


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_every_line_is_an_access_log_record(path):
    records = _records(path)
    assert records
    for rec in records:
        assert rec["msg"] == "handled request"
        assert {"method", "uri", "headers"} <= rec["request"].keys()
        assert isinstance(rec["status"], int)


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_addresses_are_documentation_or_loopback(path):
    for rec in _records(path):
        req = rec["request"]
        for field in ("remote_ip", "client_ip"):
            assert _allowed_ip(req[field]), (field, req[field])
        source = _source((req["headers"].get("User-Agent") or [""])[0])
        for value in req["headers"].get("X-Forwarded-For", []):
            for part in value.split(","):
                assert ipaddress.ip_address(part.strip()) in SOURCE_NETS[source], (source, value)
    # Anywhere else in the file, too: catches a real address in a field the scrub didn't know about.
    for text in _texts(path):
        assert _disallowed_ip_literals(text) == []


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_host_is_the_made_up_one(path):
    # Any URL anywhere (Referer, Origin, redirect_uri, resource), encoded or not, names only an allowed host.
    for text in _texts(path):
        assert "ngrok" not in text
        assert _disallowed_url_hosts(text) == []
    for rec in _records(path):
        req = rec["request"]
        assert req["host"] == FAKE_HOST
        for value in req["headers"].get("X-Forwarded-Host", []):
            assert value == FAKE_HOST
        for key, value in parse_qsl(urlsplit(req["uri"]).query):
            if key == "resource":
                assert urlsplit(value).hostname == FAKE_HOST


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_credentials_are_redacted(path):
    for rec in _records(path):
        req = rec["request"]
        for name in ALWAYS_REDACTED_HEADERS:
            for value in req["headers"].get(name, []):
                assert value == "REDACTED", name
        for value in rec.get("resp_headers", {}).get("Set-Cookie", []):
            assert value == "REDACTED"
        # The bridge's own authorization code, and Discord's authorize URL with its state, ride in Location.
        for value in rec.get("resp_headers", {}).get("Location", []):
            assert value == "REDACTED", "Location must be deleted or redacted"
        for key, value in parse_qsl(urlsplit(req["uri"]).query):
            if key in ALWAYS_REDACTED_QUERY:
                assert value == "REDACTED", key
            if key == "code_challenge":
                assert value == RFC7636_CHALLENGE
            if key == "client_id":
                assert value == PUBLIC_CLIENT_ID, "a deployment's own client id must be scrubbed"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_signatures_are_zeroed_and_dates_removed(path):
    for rec in _records(path):
        req = rec["request"]
        for value in req["headers"].get("X-Signature-Ed25519", []):
            assert value == "0" * 128
        assert "remote_port" not in req
        assert "Date" not in rec.get("resp_headers", {})


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_timestamps_are_shifted(path):
    records = _records(path)
    assert records[0]["ts"] == FIRST_TS
    for rec in records:
        for value in rec["request"]["headers"].get("X-Signature-Timestamp", []):
            assert abs(int(value) - rec["ts"]) <= SIGNATURE_TS_TOLERANCE, (value, rec["ts"])
