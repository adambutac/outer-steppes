# ADR-0005: Webhook secrets are stored in plaintext; tokens the bridge only checks are hashed

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-208 (part of MALU-199; moved from DESIGN.md, Key decision 6) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 6) in MALU-199. The decision was made in revision 3 of the design, on 2026-09-27, when the review of revision 2 asked for it to be stated as a Key decision. The rule's hashed list grew with later features: OAuth client secrets, codes, access and refresh tokens with the MCP connector (PR 5, #8, 2026-09-28), and login `state` values, browser nonces and session ids with the Discord login (revision 3.8, #11, 2026-09-29). |

## Context

The bridge keeps two kinds of secret:
- **Secrets it uses.** For each `/ask`, it signs a POST with the member's `whsec_` and sends it to the member's automation URL. It needs both in plaintext at that moment.
- **Secrets it only checks.** Reply tokens, OAuth secrets, login state, nonces and session ids are presented to the bridge, which only has to recognize them.

A third credential sits beside them in plaintext: each in-flight row's Discord interaction token, which the bridge uses for every Discord write. Storing it is its own decision ([ADR-0003](0003-interaction-tokens-are-stored.md)).

All of them live in one SQLite file on the bridge's host.

## Options

### Encrypt `whsec_` and URLs at rest, with a key on the same host

A copied database file alone wouldn't reveal them. But the bridge has to decrypt on every `/ask`, so the key has to be where the process can read it, on the same host. Anyone who compromises the host gets the key with the file. It adds key handling and rotation for little real protection.

### Store `whsec_` and URLs in plaintext; store every checked secret as SHA-256

The file and directory are owner-only (`0600` and `0700`, checked at startup). A secret the bridge only checks is stored as its SHA-256 and compared by hash, so a copy of the database can't be replayed against the bridge. CSRF tokens aren't stored at all; they're derived from the session id.

## Decision

Plaintext for what the bridge must use, hashes for everything it only checks. Encryption with a key on the same host doesn't change what a host compromise exposes, while hashing removes the checked secrets from the file entirely.

## Consequences

- A host compromise exposes every active `whsec_` and automation URL. That is the same party the threat model already trusts by design: whoever controls the bridge or its host can use every member's automation. The mitigation is running your own bridge.
- A leaked database copy can't be used to post replies through the bridge, refresh MCP tokens, or take over a login or session. A live interaction token in it can still edit that interaction's response and post followups on Discord, for at most 15 minutes from the `/ask` ([ADR-0003](0003-interaction-tokens-are-stored.md)).
- Database snapshots are as sensitive as the host, and are handled like the bot token.
- A new secret the bridge only checks is stored as a hash. A new secret it must use needs its own decision.

## Revisit if

- The host can keep a key out of the bridge's own reach, such as a hardware key or a separate signing service, so encryption would protect against more than a stolen file.
- The bridge no longer signs with the member's `whsec_`.
