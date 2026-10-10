# ADR-0010: The app is user- and guild-installable, and answers are private by default everywhere

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-29 |
| Merged | 2026-10-06 |
| Linear | MALU-209 (part of MALU-199; moved from DESIGN.md, Key decision 13) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 13) in MALU-199. The decision was made in revision 3.7 of the design, which shipped with PR 7 on 2026-09-29 (afb8e1d, #10). |

## Context

Until then the app was installed to servers only, with per-server commands. A member could use their own Grok only in a server whose admins had installed the app and allowlisted it. The bridge is about a member's own account, so being tied to particular servers was the wrong boundary.

Facts this depends on (Discord docs retrieved 2026-09-28):
- Discord applies `integration_types` and `contexts` only to global commands, so user installs need global commands.
- In a DM Discord sends a signed top-level `user` and no `member`; `authorizing_integration_owners` says whether the app was installed to the server, the user, or both.
- A user-installed app is limited to 5 followup messages per interaction in servers that didn't install it. The bridge's answers use at most 1 edit plus 4 followups.
- An answer can carry the member's personal-connector data (mail, files and so on), so where it lands matters.

## Options

### Guild installs only (revisions 1–3.6)

Each server installs the app, and `GUILD_ALLOWLIST` gates it. Simple, but a member can't use their Grok in a DM or in a server that hasn't installed the app.

### User and guild install, with a default that depends on the install (a 3.7 draft)

Answers are public where the server installed the app and private elsewhere. Piloted, and confusing: a member had to remember how the app reached each chat to know where their answer would land.

### User and guild install, private by default everywhere

Global commands usable in servers, DMs with the bot, and group DMs. Membership is the gate; `GUILD_ALLOWLIST` and `CHANNEL_ALLOWLIST` restrict only servers that installed the app. Answers are ephemeral unless the member changes their own default with `/settings`, and `/ask private:` overrides it per message.

## Decision

User and guild install, with one visibility rule everywhere: private by default, changed only by the member. Members can use their Grok wherever they are, and a personal-connector answer never lands in a chat unless the member chose that.

## Consequences

- Commands are global (`integration_types [0, 1]`, `contexts [0, 1, 2]`), and `register_commands.py --clear-guild-commands` removes the old per-server ones.
- The caller is the signed `member.user.id` in a server and the signed `user.id` in a DM; a malformed mix is refused.
- The bridge's server allowlists no longer decide alone whether it's used in a server: they apply only where the server installed the app, and a member who installed it themselves can use it in other servers too. `USER_ALLOWLIST` applies everywhere.
- Each interaction records `guild_installed`, so the worker's and `deliver_reply`'s re-checks apply `GUILD_ALLOWLIST` the same way the ACK did.
- `/settings` and the `member_settings` table hold each member's default.

## Revisit if

- Discord changes the followup limit for user-installed apps, or what it sends in DMs.
- Members ask for a per-server default, which this rule deliberately doesn't have.
