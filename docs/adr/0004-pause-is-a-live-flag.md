# ADR-0004: The kill switch is a live `paused` flag in the database

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-208 (part of MALU-199; moved from DESIGN.md, Key decision 5) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 5) in MALU-199. The decision was made in revision 3 of the design, on 2026-09-27, after the review of revision 2. |

## Context

The operator needs one control that stops everything now: no new `/ask`, no new registrations, and no answers from requests already sent. It must never block revocation, so a member can always remove their automation or leave.

In revision 2 the kill switch was an empty `GUILD_ALLOWLIST`. Configuration is process environment, so changing it meant a restart. The design said an empty allowlist withdraws in-flight replies, because `deliver_reply` re-checks it. The review found that unreachable: the restart itself marked every in-flight row failed, and interaction tokens were then kept only in memory, so no withdrawal message could be posted ([ADR-0003](0003-interaction-tokens-are-stored.md)). Members were left on "thinking…".

## Options

### Empty `GUILD_ALLOWLIST` and restart (revision 2)

No new mechanism. But it needs a restart, which drops in-flight work instead of withdrawing it, and it reuses a setting that has another job.

### Re-read the allowlist on every check

The review's other suggestion: keep the allowlist as the switch, but read it live on each `/ask`, worker step and `deliver_reply`. That needs a way to change process configuration without a restart, and still ties the emergency stop to a guild list.

### A `paused` flag in the database

`settings.paused`, set and cleared on the loopback API (`osctl pause`). Every check reads it in the same transaction as the rest of its state, so it takes effect on the next transaction. Setting it withdraws every `queued` and `posted` interaction in the same transaction.

## Decision

A live `paused` flag in the database. It stops new work and withdraws in-flight replies without a restart, and it lives beside the state it guards, so every check sees it consistently.

## Consequences

- `osctl pause` refuses new work (for example new `/ask` calls, registration, connector logins and account-page saves), and withdraws `queued` and `posted` interactions, each getting "Grok is paused. This request was withdrawn."
- "Withdrawn" means the answer is refused, not that the grok.com run is cancelled. A POST committed just before the pause may still be sent, and a delivery already in `answering` finishes.
- Revocation, `/status`, `/settings`, and removing an automation or leaving on the account page still work while paused.
- Withdrawal messages survive a stop after the pause: the finalizer sends them at the next startup while the tokens are still usable.

## Revisit if

None expected.
