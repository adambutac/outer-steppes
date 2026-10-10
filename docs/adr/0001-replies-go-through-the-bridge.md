# ADR-0001: Replies go through the bridge, authorized by a single-use reply token

| Field | Value |
| --- | --- |
| Status | Partly superseded by [ADR-0017](0017-prompts-and-answers-through-the-connector.md): the HTTP `/reply` path and that a reply token alone can post |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-207 (part of MALU-199; moved from DESIGN.md, Key decision 1) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 1) and Alternatives considered (items 1 and 2) in MALU-199. The decision was made in revision 2 of the design, on 2026-09-27, which replaced revision 1's reply path. |

## Context

A grok.com automation runs after the bridge has deferred a member's `/ask`, and its answer has to end up in Discord. Facts this depends on:
- Discord's interaction token can edit the deferred response and post followups for 15 minutes from the interaction's creation, and needs no bot permissions.
- Incoming Webhooks only post into a channel. Their URL is the credential.
- grok.com automations can make outbound HTTP requests, and custom MCP connectors work from automation runs. Both were known from the operator's prior use; the pilots of PR 4 (#7) and PR 5 (#8) confirmed them later.

## Options

### The automation posts to a per-registration Incoming Webhook (revision 1)

The bridge hands the automation a Discord webhook URL, and the automation posts the answer itself. The URL is a durable posting credential that leaves the bridge, and revoking it needs a DELETE sweep. Ephemeral replies are impossible, the bridge can't control mentions, and every user needs a webhook.

### Replies go through the bridge, MCP only

The automation answers only by calling a tool on the bridge's MCP server. It keeps every Discord credential in the bridge, but it rests entirely on the connector, while the HTTP callback relies only on outbound HTTP, which an existing integration already used.

### Replies go through the bridge, over HTTP `/reply` and MCP

The bridge mints a single-use reply token for each accepted `/ask` and sends it in the signed payload. The automation returns its answer with that token, either by POSTing to `/reply` or by calling the MCP tool `discord_reply`. Both paths call the same `deliver_reply`, and the bridge writes to Discord with the interaction token it already holds.

## Decision

Replies go through the bridge, over both HTTP `/reply` and MCP, authorized by a single-use reply token. It keeps every posting credential in one process, which makes ephemeral replies possible and lets the bridge control mentions, length and withdrawal. Keeping the HTTP path means the reply path doesn't depend on the connector, at the cost of a second, small entry point that shares one implementation with the first.

## Consequences

- No Discord credential leaves the process. The automation holds only a reply token, which can post one answer to one interaction, with no pings.
- Answers are limited to 14 minutes. The token's lifetime is capped below Discord's 15-minute interaction token, so the bridge can still post its expiry message.
- A leaked reply token lets its holder post one answer to that interaction before the real one does.
- The bridge withdraws a reply that is no longer authorized (pause, revoke, a failed route re-check) at delivery time, because it does the posting.
- Two reply paths share one function, so they can't drift apart in authorization or rendering.

## Revisit if

- Answers need to arrive after 14 minutes. A bridge-held per-channel Incoming Webhook, never sent to anyone, could post a new message that references the caller.
- Discord changes the interaction token's lifetime or what it can write.
