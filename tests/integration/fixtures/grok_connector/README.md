# Recorded grok.com connector traffic

What the real grok.com connector sent to a test bridge, captured from the
access log of the reverse proxy the bridge ran behind before
[ADR-0015](../../../../docs/adr/0015-tailscale-funnel-exposes-the-bridge.md),
and scrubbed to made-up values. The why, and when to re-record: DESIGN.md →
Testing, "Recorded traffic". That proxy is gone, so a re-recording needs a new
capture point (MALU-106).

## `connector_2026-10-02.jsonl`

One access-log record per line, in that proxy's JSON shape, 23 requests:

1. A `/status` slash command from Discord (`POST /interactions`).
2. A fresh connector connect: two unauthenticated `POST /mcp` probes (401),
   both `.well-known` discovery URLs (404 from the old proxy, so these two
   records hold its answer, not the bridge's; the bridge 404s them too), `/oauth/authorize`,
   the Discord callback, and the code exchange at `/oauth/token`.
3. An `/ask`, answered over MCP: two parallel sessions of `initialize`,
   `notifications/initialized` and `tools/list`, then a third session ending in
   `tools/call` (`discord_reply`).
4. A refresh at `/oauth/token`, made by grok.com on its own schedule 5,509 s
   after the code exchange, then a second `/ask` answered in one MCP session with
   no `tools/list`. The recording bridge left `MCP_ACCESS_TOKEN_TTL_SECONDS`
   unset, so the access token lived the default 3,600 s, and the refresh came
   about 32 minutes after it expired.

Access logs hold no bodies, so the JSON-RPC methods above are inferred from
the order, sizes and status codes, not read from the log. One size pins its
method exactly: the 54-byte calls answered 202 are
`{"jsonrpc":"2.0","method":"notifications/initialized"}`, which is 54 bytes
compact.

`tests/integration/test_recorded_traffic.py` replays this recording against
the real service (DESIGN.md → Testing, "Recorded traffic"). A re-recording
that changes the sequence fails its first test, which lists the steps the
replay expects.

### How it was scrubbed

At capture time: the proxy's defaults redacted `Authorization`, `Cookie` and
`Set-Cookie`, and its log filter replaced `code` and `state` in query strings
with `REDACTED` and deleted the `Location` response header, which carries the
bridge's authorization code and Discord's authorize URL. A new capture point
must keep the same values out. Then the scrub replaced:

| Field | Replaced with |
| --- | --- |
| `X-Forwarded-For` | Documentation ranges with a stable mapping per real address: Discord `192.0.2.x`, grok.com `198.51.100.x`, the browser `203.0.113.x` |
| Host, `X-Forwarded-Host`, the `resource` parameter | `bridge.example.test` |
| `code_challenge` | The RFC 7636 appendix B example challenge |
| `X-Signature-Ed25519` | 128 zeros |
| `ts` | Shifted so the first request is at 1800000000; gaps unchanged |
| `X-Signature-Timestamp` | The record's shifted `ts`, truncated to whole seconds (Discord's own value, a second or so off, isn't kept) |
| `Date` response header, `remote_port` | Removed |
| The proxy's own response headers: `Via` everywhere, and `Server` on the two discovery 404s it answered | Removed (2026-10-06, with the proxy; they describe it, not the bridge or grok.com) |

Kept as recorded, on purpose: `client_id=outer-steppes`, the default public
client that every deployment shares. A recording made through a deployment's
own `osc_…` client must replace it with `outer-steppes`. Also kept: the
browser's `User-Agent`, client hints
(`Sec-Ch-Ua*`), `Sec-Gpc` and `Accept-Language` on the two browser requests.
They show what grok.com's connect window really sends, and they identify a
browser version, not a person or an account. The bridge's own `Server`
response header (`Python/3.12 aiohttp/3.14.3`) is kept too: it describes the
test bridge's stack, and the replay doesn't compare it.

`tests/test_recorded_fixtures.py` checks every file in this folder and its
subfolders except this README. It refuses any format other than `.jsonl`
until checks are written for it (a body capture's HAR, for instance). In each
recording:

- every IPv4 or IPv6 address anywhere in the file, raw or percent-decoded (as
  many times as it takes), is loopback or a documentation address, and each
  `X-Forwarded-For` sits in its sender's range. The one exception is a
  Chromium-style version token ending in `.0.0.0`, such as `Chrome/154.0.0.0`,
  where the product name doesn't follow a `/`;
- every `http://` or `https://` URL anywhere, decoded the same way, names only
  `bridge.example.test`, `grok.com` or `discord.com`. A bare hostname outside a
  URL is caught only if it contains `ngrok`;
- `Authorization`, `Cookie`, `Set-Cookie`, `code` and `state` are `REDACTED`,
  `Location` is absent or `REDACTED`, the challenge is the RFC 7636 one,
  `client_id` is `outer-steppes`, and signatures are exactly 128 zeros;
- `Date` and `remote_port` are gone, the first `ts` is 1800000000, and each
  `X-Signature-Timestamp` is within 5 s of its shifted `ts`.

So a re-recording that misses one of the rules above fails CI. What it can't
check is a made-up-looking value that is in fact real (a token that happens to
look redacted, for instance), so read a new recording once before committing it.
