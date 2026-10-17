# Architecture decision records

Each file here records one decision: the problem, the options considered, what was chosen, and what that costs. [DESIGN.md](../../DESIGN.md) describes the system as it is now and links here for the reasons.

**Why this split.** DESIGN.md once did four jobs at once: what the system does, why (a Key decisions table, Alternatives considered, and reasoning scattered through its sections), history, and planning. The owner reviews through PR descriptions, and a fresh session or the `pr-reviewer` agent has to find things without context, yet one decision's reasons were spread across several places, and a reversed decision was rewritten in place, so the rejected reasoning disappeared into git history. Replacing DESIGN.md with ADRs would leave nothing that says what the system does now, which "design before behavior", the `pr-reviewer` agent and the PR template all rely on. So DESIGN.md says what, and each ADR records why, at the cost of one more place to update in a PR that makes such a decision. If ADRs end up just restating DESIGN.md, or PRs routinely skip them, the split isn't earning its cost.

## When to write one

The owner set this rule on 2026-10-07: ADRs are strictly about the architecture of what is being built, and how to go about building it is outside their scope unless it ties directly into the product.

**Scope.** An ADR records a decision about the architecture of the product: what the bridge is, what it trusts, what it stores, how it talks to Discord and grok.com, and how it's deployed and exposed. How we build it (process, workflow, development tooling, documentation, planning) is out of scope, unless the choice directly shapes the product. For example, `osctl`'s command layout is tooling, but keeping the bot token out of the bridge's container shapes the product.

**Test.** A decision in scope needs an ADR only if all four hold:
1. **It's significant to the product:** trust boundaries or security, what's stored and how, an external contract (Discord, grok.com, the admin API), runtime or deployment topology, or dependencies and licensing.
2. **It's difficult to change, or difficult to undo:**
   - **Change:** reversing it means migrating data, breaking an external contract, making operators or members redo their setup, or reworking the security model.
   - **Undo:** once shipped, its effects can't be taken back, such as data deleted, secrets exposed, runs started on members' accounts, or code released under a license.
3. **Real alternatives were weighed.**
4. **Someone could reasonably undo it without knowing why.**

**Otherwise, no ADR.** The reason goes next to the rule it explains: a line in DESIGN.md, a code comment, CLAUDE.md, or this README.

The ADR goes in the same PR as the change it decides. If the decision changes behavior, that PR also updates DESIGN.md, which keeps the "design before behavior" rule in [CLAUDE.md](../../CLAUDE.md).

## Statuses

- **Accepted:** the decision in force. An ADR is written as Accepted, with `Opened` set to the day its PR is opened and `Merged` left as `pending`. Merging the PR is the acceptance, and nobody edits the ADR for it: on `main`, the ADR merge date workflow (`.github/workflows/adr-merged.yml`) replaces `pending` with the merge date, read from git. It only ever replaces `pending`: a date already there stays, because the public repository starts from one squashed commit, whose history would give every ADR the cutover day. Both dates are kept because a design PR can wait a long time as a draft (CLAUDE.md, "A design ahead of its code"), so the day it was proposed and the day it took effect can be far apart. A PR closed without merging never lands the ADR. There's no "Proposed" status, because an undecided ADR is never merged.
- **Superseded by ADR-NNNN:** replaced by a later decision.
- **Partly superseded by ADR-NNNN:** a later decision replaced one named part, and the rest still stands. The status names the part, and the later ADR's "Supersedes" field names the same part.
- **Retired:** no longer applies and nothing replaced it, for example because the feature was removed; or it no longer meets [When to write one](#when-to-write-one), and its reasons moved next to their rules. A retired ADR stays in this folder.
- **Accepted, with a part dropped:** one named part was later dropped without a new ADR, because that part alone doesn't meet [When to write one](#when-to-write-one). The status names the part and where its reason now lives, and the rest still stands.

An ADR records a decision as it was made. Once merged, change only its Status line, broken links and typos (the workflow's `Merged` stamp aside), or replace a link that leaves this folder with what it pointed to when the ADR merged, dropping it where the text already says that ([Standing on its own](#standing-on-its-own)). Until the owner says these conventions have settled, a merged ADR may also be edited to follow them, for example shortened, as long as its decision, context, options and costs are preserved:

- **The decision changed:** write a new ADR that names the old one under "Supersedes", and set the old one's status to "Superseded by" the new one, in the same PR. If only part of it changed, name that part in both places and use "Partly superseded by".
- **Only later details changed** (a renamed setting, a moved file): leave the ADR alone. DESIGN.md describes current behavior, and it wins wherever the two differ.

## Numbering and names

Files are named `NNNN-short-title.md`, with four digits. Take the next free number; numbers are never reused. If another PR merges with the same number first, renumber yours before it merges, and update every mention of its number: DESIGN.md, CLAUDE.md, code comments, the index row and any Supersedes field.

**One exception, on 2026-10-07 (MALU-103).** The owner had the retired ADRs deleted and the rest renumbered 0001–0015, to clean things up once their reasons had moved next to their rules. It's not a pattern. From now on an ADR is retired in place and its number is never reused. An older issue, PR or commit that quotes an ADR by number alone may name a different decision; one that quotes a title or file name still identifies it, and git history before MALU-103 has the old files.

ADR-0001 to ADR-0014 came from DESIGN.md's Key decisions table in MALU-199. Their numbers have since changed, and each one's `Origin` row names the Key decision it was. Two rules applied to these migrated ADRs:

- **Opened:** when the decision was made, not when the migration PR was opened. Look first at DESIGN.md's Date row as it stood at the migration, then at `git log` for DESIGN.md (the commit that added the decision's row); both are in git history. Where neither settles it, use the migration date and say so. Their `Merged` row is the day the migrated ADR merged, filled in from git history in MALU-269, when the `Date` row was split in two.
- **Alternatives considered:** each item moved into the ADR for the decision it lost to. Items 5 (Make, Zapier, or n8n) and 6 (the xAI API with a key held by the bridge) belonged to no Key decision; they argue for the bridge's overall shape, so they stay in [DESIGN.md → Why this shape](../../DESIGN.md#why-this-shape). Leaving them there was a choice made for the migration, not a pattern: a new rejected alternative goes in the ADR for its decision, when that decision needs one.

## How ADRs and DESIGN.md divide the work

- **DESIGN.md** says what the system does and the rules it follows now, and links to the ADR for the reasons.
- **Every ADR is linked from where it applies:** the DESIGN.md section it decides. The index below lists them all.
- **An ADR** says why, what was rejected, and what was accepted as a cost, as of the decision.

## Standing on its own

An ADR makes sense without opening anything else, now and after DESIGN.md has moved on. Keep it short: only what a reader needs to understand the decision, the options and the costs.

- **Links go one way.** DESIGN.md and CLAUDE.md link to ADRs; an ADR links only within this folder. DESIGN.md changes and an ADR doesn't, so a link into it ends up pointing at text the ADR wasn't written against, or breaks.
- **State the facts the decision turned on, as of when it merges,** in its Context: a draft that waits is brought up to date before it leaves draft. The copy is never kept in sync: it records the premise, so a later reader can see when it moved and a superseding ADR is due.
- **Web sources** may be cited, with the date retrieved, but the ADR says what it relies on from them.
- **Plain mentions** (MALU-199, a PR number) are fine, as long as the ADR doesn't need them to be understood.
- **No permalinks** to DESIGN.md at a commit: the public repository starts from one squashed commit, so those hashes won't exist there.

Rules about ADRs themselves, like these, live in this README and the template, not in an ADR of their own (owner, 2026-10-07).

## Rules that apply here too

- No tokens, secrets, `whsec_` values, automation URLs, prompt text or real ids, as anywhere else in the repository.
- An ADR never describes an open security finding. DESIGN.md → Security → Vulnerability handling says where one is recorded instead.

## Starting one

Copy [template.md](template.md) to the next free number, fill it in, and add a row to the index below.

## Index

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-replies-go-through-the-bridge.md) | Replies go through the bridge, authorized by a single-use reply token | Partly superseded by ADR-0017: the HTTP `/reply` path and that a reply token alone can post |
| [0002](0002-never-sign-twice.md) | Never sign twice; 429 and 503 count as not sent | Accepted |
| [0003](0003-interaction-tokens-are-stored.md) | Interaction tokens are stored in the database; prompts are not | Partly superseded by ADR-0017: the reason prompts stay out of the database (they still do), the stored prompt excerpt, and that `posted` replies always survive a restart |
| [0004](0004-pause-is-a-live-flag.md) | The kill switch is a live `paused` flag in the database | Accepted |
| [0005](0005-webhook-secrets-in-plaintext-tokens-hashed.md) | Webhook secrets are stored in plaintext; tokens the bridge only checks are hashed | Accepted |
| [0006](0006-self-serve-membership-behind-discord-login.md) | Membership is self-serve behind Discord login; webhook secrets go only to the bridge's page | Partly superseded by ADR-0016: self-serve membership as the default |
| [0007](0007-one-replica.md) | One replica | Accepted |
| [0008](0008-server-never-holds-the-bot-token.md) | The server never holds the bot token | Accepted |
| [0009](0009-no-role-grants.md) | Role grants are not supported | Accepted, with a part dropped: the startup refusal of `ROLE_GRANTS_ENABLED=true` (DESIGN.md → Config) |
| [0010](0010-user-and-guild-install-private-by-default.md) | The app is user- and guild-installable, and answers are private by default everywhere | Accepted |
| [0011](0011-discord-login-ties-the-connector-to-a-member.md) | Discord login ties the grok.com connector to a member | Accepted |
| [0012](0012-members-and-automations-are-separate.md) | Members and automations are separate | Accepted |
| [0013](0013-license-agpl-3-0-only.md) | License: AGPL-3.0-only | Accepted |
| [0014](0014-run-your-own-bridge.md) | Run your own bridge | Partly superseded by ADR-0016: the defaults are unchanged for now |
| [0015](0015-tailscale-funnel-exposes-the-bridge.md) | Tailscale Funnel is the only supported way to expose the bridge | Accepted |
| [0016](0016-personal-by-default-claimed-through-a-link.md) | A bridge is personal by default, and its owner claims it through a one-time link | Accepted |
| [0017](0017-prompts-and-answers-through-the-connector.md) | Prompts and answers pass only through the member's connector | Partly superseded by ADR-0018: how a run that calls through another member's connector ends: with `unknown` and a timeout, a run per `/ask` until the member gives up |
| [0018](0018-another-members-connector-bans-the-signing-secret.md) | A run that calls through another member's connector ends the ask and bans the token owner's signing secret; a trigger URL is stored once | Accepted |
| [0019](0019-the-bridge-sends-the-runs-instructions.md) | The bridge sends the run's instructions in the trigger body | Accepted |
| [0020](0020-grok-bot-webhooks-use-a-bearer-key.md) | Grok Bot routines are a second automation kind, authenticated with a bearer key | Accepted |
