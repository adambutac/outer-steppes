# ADR-0015: Tailscale Funnel is the only supported way to expose the bridge

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-10-06 |
| Merged | 2026-10-06 |
| Linear | MALU-198 |
| Supersedes | None |

## Context

Discord and grok.com reach the bridge's public listener over HTTPS, so something has to give it a public HTTPS URL. Whatever terminates TLS sees in plaintext: prompts and answers; interaction, reply and MCP access tokens; the 180-day refresh tokens in `/oauth/token` responses; session cookies; and the `whsec_` value and automation URL a member enters on `/account`. The bot token, the admin listener and the Discord client secret never cross it.

Until now, DESIGN.md described Caddy terminating TLS on a VM, and `scripts/dev.py tunnel` used ngrok for development. The repo has `deploy/Caddyfile.example` and a test that reads it. Caddy contributed TLS, a route allowlist as a second layer behind the two listeners, log redaction of `code`, `state`, `Location` and `Set-Cookie`, and optional per-IP rate limits through a plugin, left commented out.

Most operators are expected to run a personal bridge at home ([ADR-0014](0014-run-your-own-bridge.md)), on a machine that usually has no public IP or open port.

Facts this decision depends on:
- **Tailscale's docs** ([Funnel](https://tailscale.com/kb/1223/funnel), retrieved 2026-10-06):
  - Funnel's relays don't decrypt traffic; tailscaled terminates TLS on the user's own device.
  - Certificates are issued automatically, for `<machine>.<tailnet>.ts.net` only.
  - Ports 443, 8443 and 10000 only, with fixed bandwidth limits, on all plans.
  - It needs MagicDNS, HTTPS certificates and the `funnel` node attribute; `tailscale funnel` walks through enabling the attribute.
  - `tailscale funnel --bg` keeps its configuration across restarts.
- **The MALU-200 pilot**, 2026-10-06, on the owner's machine with the bridge container (full results in MALU-200):
  - `tailscale funnel --bg 127.0.0.1:8080` served the bridge over TLS, admin paths returned 404, Discord accepted the `ts.net` Interactions endpoint, and `/ask` worked end to end.
  - A header-echo server on the host saw peer `127.0.0.1`. Funnel adds `X-Forwarded-For` with the client's address.
  - While tailscaled's log stream was watched, requests through Funnel produced no log lines, including one carrying a made-up `code` and `state` in its query.

## Options

### Caddy on a host with a public IP

No third party sees plaintext, and Caddy adds the route allowlist, log redaction and optional rate limits. But it needs a public IP, an open port and a domain, which most home setups don't have or shouldn't open, and a second piece of software, with its config, to maintain.

### A tunnel that terminates TLS (ngrok, Cloudflare Tunnel)

One command, no open port. The provider decrypts every request, prompts, tokens and `whsec_` included. Common practice, but this bridge's point is that each member's credentials stay with the person they trust ([ADR-0014](0014-run-your-own-bridge.md)). ngrok's inspector and cloud traffic capture also have to be kept off by hand.

### A TLS-passthrough tunnel with Caddy at home

An ngrok TLS endpoint, or a small VPS forwarding TCP over WireGuard or SSH, with Caddy terminating TLS at home. No third party sees plaintext, but it needs a domain, DNS-01 certificates, Caddy, and possibly a paid plan or a VPS: the most moving parts of any option.

### Tailscale Funnel

TLS terminates on the user's machine, so Tailscale's relays carry only ciphertext. No public IP, open port, domain or router setup; one command. The costs:
- a Tailscale account
- a `ts.net` hostname the user can't choose
- bandwidth limits
- Tailscale sees metadata (client IPs, timing and volume). It runs the `ts.net` DNS and certificate issuance, so in principle it could get a certificate for the user's hostname and intercept actively, which would show in Certificate Transparency logs.

### Support several, and let the user choose

The owner first leaned this way: ngrok for ease, Caddy for the smallest surface. Each needs its own docs, tests and support, and they would pull the documented setup in different directions.

## Decision

Tailscale Funnel, and only Funnel: the only option as easy as a tunnel that gives no third party plaintext. One supported path keeps the docs, `scripts/dev.py` and the tests on one setup. The README may name alternatives a user can set up themselves (Caddy, ngrok, Cloudflare), but they aren't implemented, tested or documented step by step.

## Consequences

- **What replaces Caddy's extras:**
  - **Route allowlist:** the two listeners were already the control, so admin routes stay unreachable.
  - **Body size:** the bridge caps request bodies itself, except `/oauth/token`, which relies on aiohttp's 1 MiB default rather than Caddy's 256 KB. Accepted: it's a small form, and 1 MiB is still bounded.
  - **Log redaction:** Funnel logged no requests in the pilot. That was observed, not documented by Tailscale.
- **No rate limiting at the edge, accepted for personal bridges too.** Funnel has no per-IP rate limits, and Caddy's were optional and off by default.
  - Guessing a reply token or code is hopeless: they're random, short-lived and single use.
  - A flood still works. The `ts.net` hostname is public in Certificate Transparency logs, and `/oauth/authorize` stores a pending row before anyone signs in, so `USER_ALLOWLIST` doesn't stop anyone filling `OAUTH_MAX_PENDING`. While it's full, new connector connections fail; existing connections and `/ask` keep working. The remedy is limits inside the bridge (MALU-203).
  - Open for MALU-203: Funnel passes the client address in `X-Forwarded-For`, but it connects from a local address any other process on the host can share, so the peer address can't tell Funnel apart. Whether Funnel replaces or appends a client-supplied `X-Forwarded-For` is unverified.
- **Development matches the supported setup:** `scripts/dev.py tunnel` moves from ngrok to Funnel (MALU-202), and ngrok stops being supported for development too.
- **Caddy is removed:** `deploy/Caddyfile.example`, its test, and the Caddy text in the Dockerfile, README and DESIGN.md (MALU-201). The recorded connector traffic was captured from Caddy's access log, so a re-recording needs a different capture point.
- **Hostname:** `<machine>.<tailnet>.ts.net`. Renaming the machine changes it, and Discord's endpoint and grok.com's connector then have to be updated. A stable hostname is decided with running the bridge only in a container (MALU-204), together with whether Tailscale may run in its own container beside the bridge. Until then Tailscale runs on the host and the systemd layout stays supported. If MALU-204 makes the container the only supported way, running outside one, or in a VM, stays possible, but the user does that work.
- **The operator now trusts Tailscale** for availability and for not intercepting actively, and Tailscale sees the traffic metadata. DESIGN.md's threat model records this.
- **DESIGN.md** → Deployment and rollout, Architecture (Exposure, Request logging) and the threat model describe Funnel.

## Revisit if

- Tailscale starts decrypting Funnel traffic, changes Funnel's terms, or limits bandwidth enough to break replies.
- Shared bridges become common enough that per-IP rate limits at the edge matter more than setup simplicity.
- Users need a custom domain.
- Funnel starts logging request lines, query strings or headers. tailscaled sends its logs to Tailscale by default, and the no-logging fact rests on one observation, so re-check it after tailscaled upgrades.
