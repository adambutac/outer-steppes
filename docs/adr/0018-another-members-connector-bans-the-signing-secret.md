# ADR-0018: A run that calls through another member's connector ends the ask and bans the token owner's signing secret; a trigger URL is stored once

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-10-08 |
| Merged | 2026-10-09 |
| Linear | MALU-104 |
| Supersedes | [ADR-0017](0017-prompts-and-answers-through-the-connector.md), in part: how a run that calls through another member's connector ends: with `unknown` and a timeout, a run per `/ask` until the member gives up |

## Context

For each accepted `/ask`, the bridge signs one POST to the asking member's grok.com trigger. It carries only a single-use reply token, 32 random bytes that are never logged. The run fetches the prompt with the `get_prompt` tool and answers with `discord_reply`, both on the bridge's MCP server, which grok.com reaches as the Outer Steppes connector. Both tools need the reply token and an access token bound to the same member as the ask ([ADR-0017](0017-prompts-and-answers-through-the-connector.md)).

Facts this depends on, as of this date:
- **Only the owner's trigger is sent the token.** The bridge sends a reply token in one place only: the signed POST to the trigger of the member who asked, the token's owner. Apart from the bridge, only runs of that trigger receive it, and they run in the grok.com account that owns the trigger. A run can still leak it, for example through a prompt injection in its owner's own run; the threat model already assumes a stolen reply token.
- **The bridge knows both members but not the run.** The token names its owner's ask, and the access token names the connector's member through their Discord sign-in, but nothing says which trigger started the run. A run of the owner's trigger in an account holding another member's connector looks the same as that other member's own run fed the owner's token, for example by a prompt injection in a message or page the run reads.
- **A connector belongs to one member.** Its tokens belong to the Discord user who signed in when it was added, or to the member the operator issued a client for ([ADR-0011](0011-discord-login-ties-the-connector-to-a-member.md)).
- **Several connectors in one account aren't supported, but nothing prevents them.** grok.com refuses only an exact duplicate name. On 2026-10-08 the owner tested two Outer Steppes connectors, signed in as two different Discord users, serving the same automation; Grok picked one for each run, and runs that picked the other user's got `unknown` and timed out.
- **A mismatch is usually a setup mistake;** otherwise a leaked or fed reply token, or a leaked trigger. A run that presents a live reply token through a connector bound to a different member runs in an account that holds another member's Outer Steppes connector: a trigger saved from someone else's account, a connector added while signed in to the wrong Discord account, or several connectors in one account as above.
- **Saving an automation always inserts a new row,** even with identical values, so saving the same values again changes nothing in grok.com.
- **A trigger's secret can be rotated in grok.com, keeping its URL** (owner confirmed, 2026-10-09). The bridge already tells operators to rotate after a host compromise. A new automation also gets a new secret.
- **Until now such a call got `unknown` and wrote nothing.** The ask stayed open, and the member waited out the 14-minute reply window to see a timeout, on every ask, while the other account spent a run each time.
- **The bridge stored no evidence of it.** Nothing records which connector a run used, so a lasting effect needs a stored mark.
- **The owner asked for it** on 2026-10-08: deny further requests after `get_prompt` fails because of a reply-token mismatch. On 2026-10-09 the owner chose banning the signing secret over a block that saving the same values lifts, banning only the reply token's owner, and a trigger URL never stored twice, so that a duplicate means creating a new automation.

## Options

### Keep `unknown` and the timeout

No change. Nothing is stored and no member's asks are refused, but the member learns nothing for 14 minutes per ask, and the other account keeps spending runs.

### End the ask at once, without a lasting block

The mismatch ends the ask with a message that says what's wrong. Nothing is stored. Each later ask still spends a run in that account and ends the same way.

### End the ask and block the caller's membership

Punishes the member whose connector called, even one whose secret never leaked. Anyone holding a member's trigger URL and secret could block that member: save the trigger as their own automation and ask, so the victim's own account calls with the victim's connector. Anyone could also block a member by feeding their own live token into that member's run.

### End the ask and block the token owner's automation until it's saved again

The mismatch ends the ask and marks the automation row; its member's `/ask` is refused until they save the automation again, reconnect, or remove it. Saving the same values again lifts it, so the member can undo the block without changing anything in grok.com, where the mistake is, and the next ask spends another run there.

### End the ask and ban both secrets

Bans the token owner's secret and the connector's member's, on the reasoning that the connector's member's setup is compromised too. Anyone with a live token of their own could then get any member's secret banned by getting that token into the member's run, with nothing of the member's leaked, because the bridge can't tell such a run from a wrong setup.

### End the ask and ban the token owner's signing secret

The mismatch ends the ask and stores a hash of the token owner's automation's secret as banned; the connector's member is only warned. `/ask` is refused while the member's active automation has a banned secret, and no one can save that secret again. The member lifts it by rotating the secret in grok.com, or creating a new automation, and saving that, so the step that lifts it happens at the trigger whose runs reached the wrong connector. It can be caused only with a live reply token presented through another member's connector, and it bans only that token's owner: their own trigger's run in an account holding someone else's connector, which they chose and which could already make every ask fail by not answering; a thief presenting a token stolen from them; or the owner feeding their own token into another member's run, which bans only the feeder. It costs a table that's never cleaned up, a new state-machine ending, a write in `get_prompt` and `deliver_reply`, a check on every save and every `/ask`, and a ban a member must lift in grok.com.

### Trigger URLs: allow duplicates, or never store one twice

With token-owner bans alone, someone holding a member's leaked trigger URL and secret can save it next to the member's own automation and ask, which bans the leaked secret; that's intended, but they can do it while the member is still using the trigger. Never storing a URL twice stops that while the member has it saved, and stops members sharing a trigger. Its cost: saving checks only the secret's shape, so whoever saves a URL first holds it, even with a wrong secret, and the bridge can't tell the real owner. The alternatives were to let a banned automation release its URL (the squatter with a wrong secret is never banned, so it fixes only the leaked case) or to ban URLs as well as secrets (it doesn't stop squatting either). The owner chose never storing a URL twice, with a new automation as the answer to any duplicate (2026-10-09): a new automation always has a URL nobody holds, and a URL someone else holds is one the member shouldn't trust anyway.

## Decision

End the ask and ban the reply token owner's signing secret, for both `get_prompt` and `discord_reply` calls, warn the connector's member, and allow each trigger URL for at most one member (owner, 2026-10-08 for `get_prompt`, 2026-10-09 for the rest; `discord_reply` because a run that skips the fetch is the same evidence). It tells the member at once what to fix, stops the waste, makes them change something in grok.com before asking again, and bans only a member whose own token was presented: sent to their own trigger, stolen from it, or fed by them. A trigger URL never stored twice stops a leaked trigger being used from a second membership while its owner has it saved.

## Consequences

- A call through another member's connector, with the token of a `posted` ask inside its window, now writes: it ends the ask (`posted → failed`) with a message naming the cause, stores the SHA-256 of the token owner's automation's secret key in `banned_secrets`, and marks the calling membership, whose status surfaces then warn for up to 7 days or until they connect again. The caller still gets `unknown`, and no prompt or answer moves.
- A trigger URL is never stored twice. A save of a URL another member's automation holds, banned or not, is refused (`url_in_use`), and the member is told to create a new automation; members can no longer share a trigger. **Accepted:** someone who knows only a member's URL can save it first with any well-formed secret, which runs nothing, and the member creates a new automation. The refusal is logged with both memberships, so the operator can find the holder.
- While a member's active automation has a banned secret, their `/ask` is refused and their status surfaces warn. Saving that secret is refused on the account page and through the admin API, for every member. Only saving a secret that isn't banned lifts it; a reconnect, saving the same values, or waiting doesn't.
- There's no unban, not even by the operator: a banned secret is treated as compromised, so the only fix is a rotated secret or a new automation (owner, 2026-10-09).
- The ban is linked to no member or automation and is never deleted, so removing the automation, leaving and joining again, or another member saving the same trigger doesn't get around it.
- **Intended:** whoever steals a live reply token can present it through any other member's connector, their own (on a shared bridge, anyone admitted can join) or one whose access token they stole, which ends that ask and bans its owner's secret; the connector's member is only warned. Each use needs a live token (the ask still `posted`, at most 14 minutes), ends one ask, costs the owner a secret rotation in grok.com, and is logged with both memberships; a leak that keeps happening bans each new secret until it's fixed. The token carries no URL or secret, so the ban takes nothing from the thief. The reason is that the bridge can't tell a thief's call from a wrong setup's, and the owner ruled the ban the intended outcome on 2026-10-09.
- **Intended:** whoever holds a member's trigger URL and secret can save it as their own automation, while the member doesn't have it saved, and ask. The member's own account runs it through the member's own connector, which bans the leaked secret as the token owner's. The member can't save that secret again, and since the thief's automation still holds the URL, the member creates a new automation. That person could already start runs in the member's account, and rotating the leaked secret, which lifts the ban, is the right response to the leak anyway.
- A member's run fed another member's live token, by a prompt injection for example, bans only the feeder's secret. The member whose connector made the call is warned and needs to do nothing.
- An account with several Outer Steppes connectors bans the token owner's secret the first time a run picks another member's. That setup isn't supported; the messages tell the member to keep only their own connector. A member who saves a new secret without fixing the setup is banned again by the next wrong pick, at the cost of one run.
- `automations` gains `secret_hash`, set at insert like every other value, so the ban checks (on `/ask`, in the worker and on the status surfaces) and the foreign ending's ban compare or copy hashes and never read the plaintext secret, which stays where it was: the database and the worker's memory for one POST.
- A token with no row, or the token of an ended ask, stays plain `unknown` with nothing written.
- The operator sees each event as a warning log line, with both memberships, and in `bridge_foreign_connector_total`.

## Revisit if

- Members need to share one grok.com account or one trigger.
- Several connectors in one account become a setup members are told to use, or grok.com starts identifying the account behind a run.
- Stolen reply tokens are used to ban members' secrets in practice.
- Bans turn out to hit members whose setup was right.
