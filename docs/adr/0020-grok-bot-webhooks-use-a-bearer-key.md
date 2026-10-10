# ADR-0020: Grok Bot routines are a second automation kind, authenticated with a bearer key

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-10-09 |
| Merged | 2026-10-09 |
| Linear | MALU-284 |
| Supersedes | None |

## Context

Each `/ask` sends one POST to the member's automation, which runs, fetches the prompt with `get_prompt` and answers with `discord_reply` through the member's Outer Steppes connector ([ADR-0017](0017-prompts-and-answers-through-the-connector.md)). The body carries the run's instructions ([ADR-0019](0019-the-bridge-sends-the-runs-instructions.md)). Until now the one kind was a grok.com automation trigger, signed with its `whsec_` secret (Standard Webhooks).

Facts this depends on, as of this date:
- **Grok Bot routines have webhook triggers** at `https://api2.cursor.sh/automations/webhook/<uuid>`, Cursor's automations service, which hosts them. Saving a routine shows the URL and a key, `crsr_` followed by 64 hex characters.
- **The key goes in an `Authorization: Bearer` header on every request.** There is no body signature.
- **Grok Bot runs can use the Outer Steppes connector,** and followed the body's instructions: in the owner's live test on 2026-10-09 the POST got HTTP 200, the run called `get_prompt` and then `discord_reply`, and the answer reached Discord in 46 seconds.
- **The forum reports outages that returned 401.** The bridge sends one POST per ask and never retries, so a 401 fails that ask.

## Options

### Leave Grok Bot out

Members who use Grok Bot can't use the bridge.

### Add it as a second kind of automation

A second URL-policy provider, its own kind and secret format, and a bearer header instead of a signature. Everything after the POST is shared: the payload, the connector, bans and the URL rule.

### Put a signing proxy in front of it

The bridge would sign as today and a proxy would swap the signature for the key. That adds a component and still hands the proxy the key.

## Decision

Add Grok Bot as a second kind, `grok_bot_webhook`, chosen by the URL's provider (owner, 2026-10-09).

## Consequences

- The URL decides the kind: `api2.cursor.sh/automations/webhook/<uuid>` is Grok Bot, with the same exact-match, canonical-form and DNS rules as grok.com. The secret must be the kind's format, so a `whsec_` with a Grok Bot URL, or a `crsr_` key with a grok.com URL, is refused.
- A Grok Bot POST carries the key as a bearer and no signature. TLS protects the key and the body in transit; nothing protects the body's integrity beyond that. The body holds no prompt and no Discord ids, and anyone holding the key could already start runs.
- The key is stored in plaintext like a `whsec_`, because the bridge sends it, and is held in memory only for the POST. Bans hash it as it stands, since it has one spelling.
- A 401 from Grok Bot says "The automation rejected the key.", and is never retried, outages included.
- `api2.cursor.sh` is Cursor's host, not Grok Bot's: a change on their side can move it, and adding or changing a provider stays a code change.

## Revisit if

- Grok Bot's webhooks move host or path, gain a signature, or start rejecting a bearer key.
- 401 outages turn out to be common enough to need retries.
