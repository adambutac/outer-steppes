# ADR-0016: A bridge is personal by default, and its owner claims it through a one-time link

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-10-07 |
| Merged | 2026-10-07 |
| Linear | MALU-259 (part of MALU-231) |
| Supersedes | Part of [ADR-0014](0014-run-your-own-bridge.md): its consequence that the defaults (`SELF_SERVE`, `MAX_MEMBERS`) are unchanged for now. Part of [ADR-0006](0006-self-serve-membership-behind-discord-login.md): self-serve membership as the default |

## Context

The recommended setup is one bridge per person, with its owner as the only member ([ADR-0014](0014-run-your-own-bridge.md)). Joining gives the operator power over the member's grok.com account. A connected connector lets a modified bridge steer that member's runs, and a webhook lets the operator start them with any prompt. So a bridge should hold only people who meant to trust its operator.

Facts as of 2026-10-07:
- Out of the box, any Discord user could join a bridge that has Discord login on. `SELF_SERVE` defaulted to on, `USER_ALLOWLIST` was empty and `MAX_MEMBERS` was 100. A personal bridge depended on its owner remembering to set `USER_ALLOWLIST` to their own Discord id, which the setup docs asked for and nothing enforced.
- The bridge is reached at a `*.ts.net` name that appears in public Certificate Transparency logs as soon as Funnel is up. A stranger can find it before the owner has signed in.
- The server never holds the bot token ([ADR-0008](0008-server-never-holds-the-bot-token.md)). It can't ask Discord who owns the application, and an application can be owned by a team rather than one user.
- The operator already runs one command to start the bridge, `osctl container up`, and holds the admin API's bearer.

## Options

### Keep self-serve as the default, and document `USER_ALLOWLIST`

No new code. A forgotten step leaves the bridge open to any Discord user, and nothing tells the owner.

### Refuse to start without `USER_ALLOWLIST`

Safe, but the owner has to find their own Discord user id, a developer-mode step, before the bridge will start at all.

### Turn `SELF_SERVE` off by default

Only `osctl member add` adds members, so the owner has to register through `osctl`, pasting their trigger URL and secret into a prompt instead of the account page. The owner rejected this and the option above because both add setup friction and push the owner toward `osctl`.

### First sign-in wins

The first Discord user to sign in becomes the owner. Because of the CT logs, a stranger can sign in first.

### Read the application's owner with the bot token

`osctl command register` holds the token and could look up the owner. The server would then need that id through a new channel, and a team-owned application has no single owner.

### A one-time claim link

While the bridge has no members, the operator mints a claim link through the admin API, and whoever opens it and signs in with Discord becomes the first member. It needs no Discord id, and the trigger URL and secret stay on the account page. The link is a secret, so it travels only from the admin API to the operator's terminal, never through the logs.

## Decision

A bridge is personal by default, and its owner claims it through a one-time link. The link is the only way to make the first member without `osctl member add`, and it beats every other option on setup friction without trusting whoever arrives first.

The details decided with it:
- **Three modes in one setting,** `MEMBERSHIP`:
  - `personal`, the default when Discord login is on: the claim, then nobody else joins on their own.
  - `shared`: the claim, then anyone may join, subject to `USER_ALLOWLIST` and `MAX_MEMBERS`.
  - `operator`, the default without Discord login: only the operator adds members and sets automations, and there's no claim link.

  `SELF_SERVE` is gone. A boolean couldn't name three modes, and "unset" and "false" would have meant different things.
- **Sharing is a setting plus a restart,** `MEMBERSHIP=shared`, chosen by the owner. Rejected: a live `osctl` switch (one more command) and a switch on the account page, which needs an owner role on memberships and a new form inside the top-risk web surface.
- **Unclaimed means no active members.** No flag is stored. A bridge whose last member leaves or ages out is closed again until a new claim.
- **The link's secret** is 256 random bits, stored only as its SHA-256 ([ADR-0005](0005-webhook-secrets-in-plaintext-tokens-hashed.md)). It lasts 1 hour, a new link replaces the old one, and creating any member deletes it. The pending Discord login records which link started it, so a replaced link fails even mid-login.

## Consequences

- Out of the box, nobody can join a new bridge until its owner claims it, and a stranger who finds the name in the CT logs is refused.
- Whoever holds a live link within its hour can claim the bridge. It's shown once, in the operator's terminal, and it's used up by the first claim.
- Sharing is a deliberate restart with a changed setting, never a default.
- An owner who leaves, or idles out, of their own personal bridge needs a new link (or `osctl member add`) to come back.
- `MAX_MEMBERS` matters only for a shared bridge.
- The operator mints a link with `osctl bridge claim`. Having `osctl container up`, the command the owner already runs, print one on an unclaimed bridge is planned separately (MALU-260); until then the owner runs `osctl bridge claim` themselves.
- DESIGN.md (Members and automations, Claiming the bridge, Config, Threat model), the README and the code changed with this ADR.

## Revisit if

- The server learns its application's owner without holding the bot token.
- Owners often lose the link before claiming, or want sharing switched without a restart.
