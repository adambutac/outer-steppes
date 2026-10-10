# ADR-0003: Interaction tokens are stored in the database; prompts are not

| Field | Value |
| --- | --- |
| Status | Partly superseded by [ADR-0017](0017-prompts-and-answers-through-the-connector.md): the reason prompts stay out of the database (they still do), the stored prompt excerpt, and that `posted` replies always survive a restart |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-208 (part of MALU-199; moved from DESIGN.md, Key decision 4) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 4) in MALU-199. The decision was made in revision 3 of the design, on 2026-09-27, after the review of revision 2. Revision 3.1, the same day, added the final-message outbox, which is why a token now outlives the row's terminal transition until its final message is sent. |

## Context

Every Discord write the bridge makes (the answer's PATCH of `@original`, followups, and the messages for withdrawal, expiry and restart) uses the interaction token Discord sends with the `/ask`, because the server holds no bot token ([ADR-0008](0008-server-never-holds-the-bot-token.md)). The token lasts 15 minutes. The answer can arrive up to 14 minutes after the ACK, and it arrives on `/reply` or the MCP tool, not in the task that sent the POST ([ADR-0001](0001-replies-go-through-the-bridge.md)).

Revision 2 kept the interaction token and the prompt in memory only. The review found that this didn't hold together:
- `deliver_reply` and the sweeper both need the token after the worker that sent the POST has finished.
- On startup every in-flight row was marked failed, with no token left to tell the member. Every deploy or crash left members on Discord's "thinking…" for the rest of the 15 minutes.
- The documented kill switch, emptying the allowlist and restarting, couldn't post its withdrawal message either, because the restart had already lost the tokens ([ADR-0004](0004-pause-is-a-live-flag.md)).

## Options

### Keep tokens in memory only (revision 2)

Nothing new is stored at rest. Every restart strands each in-flight `/ask` on "thinking…": its reply can't land and no message explains why. A late reply to such a row gets 410.

### Keep tokens in one process-wide map

The review's first suggestion: one in-memory map keyed by `interaction_id`, shared by the worker, `deliver_reply` and the sweeper. That fixes the replies and messages that need the token after the worker has finished, but a restart still loses every token, so deploys and crashes still strand members.

### Store the token in the row, encrypted

The review also raised this. The key would have to sit on the same host as the file, which doesn't change what a host compromise exposes; that is the same reason `whsec_` isn't encrypted ([ADR-0005](0005-webhook-secrets-in-plaintext-tokens-hashed.md)).

### Store the token in the row, in plaintext, until it's no longer needed

The token is written at ACK and cleared when the row is answered, or when its final message has been sent or can no longer be sent. Prompts still stay in memory only. The token is stored in plaintext, like the `whsec_` values beside it ([ADR-0005](0005-webhook-secrets-in-plaintext-tokens-hashed.md)).

## Decision

Store the interaction token in the database until the row is answered or its final message is settled. The file already holds every member's `whsec_`, a far stronger credential than a token that lasts 15 minutes and reaches only one interaction: its response and followups. Prompts aren't needed after the POST, so they stay out of the database.

## Consequences

- After a restart, `posted` replies can still land, and withdrawal, expiry and restart messages can still be posted. Pausing and then stopping the process doesn't lose the withdrawal messages.
- A crash in `answering` after chunk 1 has landed leaves the partial answer in place.
- The database holds live 15-minute credentials. `interaction_token` is non-null only while the row is in flight or has a pending final message.
- The database stores only a 200-character, one-line `prompt_excerpt`, not the prompt.

## Revisit if

- Discord offers a way to edit an interaction response that doesn't need the token, such as a short-lived credential the bridge can mint.
- The database file stops holding `whsec_` values, so a token would become the strongest secret in it.
