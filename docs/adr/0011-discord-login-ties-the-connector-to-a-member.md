# ADR-0011: Discord login ties the grok.com connector to a member

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-29 |
| Merged | 2026-10-06 |
| Linear | MALU-209 (part of MALU-199; moved from DESIGN.md, Key decision 14) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 14) and Alternatives considered (item 9) in MALU-199. The decision was made in revision 3.8 of the design, on 2026-09-29 (beded01, #11), and built in PR 8a (#14). |

## Context

grok.com's custom-connector form requires OAuth, with fields for a client id, a client secret, and endpoints the bridge serves. From PR 5 each member got their own client from the operator (`osctl mcp-client`), and holding its secret was what tied a connector to that member. The bridge needs the tie so that an MCP token can only deliver answers for its own member's interactions.

Facts this depends on. Revision 3.8 was written with the first two as open questions; a login probe against the real grok.com form and Discord confirmed them the same day:
- grok.com accepts token auth method `none` with an empty secret. `OAUTH_PUBLIC_CLIENT_SECRET` stays as a contingency in case that changes.
- grok.com's connect window follows a redirect to Discord's consent screen and back, and a `SameSite=Lax` cookie survives the trip.
- grok.com sends PKCE `S256` on every authorize request (the probe for PR 5, #8).
- Discord OAuth2 with the `identify` scope tells the bridge which Discord user is in the browser. It needs the application's client secret, which is not the bot token and can't act as the bot.
- Every Discord command already carries a signed user id, so after login the bridge never needs Discord's token again.

## Options

### Per-member client secrets from the operator (revisions 3.5–3.7, as the only way)

Works without any login, and needs no new server secret. But the operator has to hand each member a secret, and a connector belongs to a member only because the right secret reached the right person.

### One public client with Discord login

Every member enters the same public values in grok.com: client id `outer-steppes`, token auth method `none`, and PKCE. The bridge's authorize step detours through Discord login and binds the resulting tokens to the signed-in user's membership. OAuth 2.1 allows public clients when PKCE is required, and the bridge already required it.

### Keep the member's Discord OAuth token

A variant of the above: store Discord's token after login. Rejected: every command already carries a signed user id, and an `identify` token can do nothing else, so it would be an unused live credential in the database, plus a refresh job.

## Decision

One public client with Discord login, discarding Discord's token after one `/users/@me` call. Per-member clients stay as a fallback. No secret has to be handed to each member, and a connector provably belongs to the Discord user who connected it.

## Consequences

- The server gains one secret, `DISCORD_CLIENT_SECRET`. It isn't the bot token ([ADR-0008](0008-server-never-holds-the-bot-token.md)) and can't act as the bot.
- The membership is chosen by the Discord identity, never by anything in the request. A single-use `state`, a browser-binding `__Host-os_login` cookie, and PKCE stop a forwarded link from connecting anyone else.
- `/oauth/authorize` is reachable by anyone who knows the public client id, so pending logins are capped (`OAUTH_MAX_PENDING`); someone can keep the cap full and stop new connections, an accepted risk.
- Discord's token is discarded without revoking it, because revoking cancels the member's whole authorization of the app, which may include their user install.
- Removing `DISCORD_CLIENT_SECRET` or renaming the public client disconnects every member who connected through it, at their connection's next refresh; access tokens already issued work until they expire.
- It made self-serve membership possible: the same login can create a member ([ADR-0006](0006-self-serve-membership-behind-discord-login.md)).

## Revisit if

- grok.com's connector form stops accepting a public client or a redirect through another site.
- grok.com offers another way to authenticate a connector, such as an API key.
