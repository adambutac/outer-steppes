# ADR-0009: Role grants are not supported

| Field | Value |
| --- | --- |
| Status | Accepted, with a part dropped: the startup refusal of `ROLE_GRANTS_ENABLED=true` in Consequences; the reason a leftover value is ignored lives in DESIGN.md → Config |
| Opened | 2026-09-29 |
| Merged | 2026-10-06 |
| Linear | MALU-210 (part of MALU-199; moved from DESIGN.md, Key decision 11) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 11), its "Why role grants are not supported" section, and Alternatives considered (item 7) in MALU-199. The decision was made in revision 3.7 of the design, which shipped with PR 7 on 2026-09-29 (afb8e1d, #10). |

## Context

Since revision 2, the design had planned **role grants**: an owner could share their automation with a Discord role, so that anyone holding the role could `/ask` it. From revision 3.2, grants sat behind `ROLE_GRANTS_ENABLED`, off by default, and were planned for their own PR. The retired design was kept in DESIGN.md as a record (Role grants flag, Grants, Confused deputy) until MALU-223 removed those sections; git history has them.

A grant would let other Discord members drive the owner's consumer Grok, with the owner's connectors and history. xAI's terms say:

- **Consumer Terms of Service:** "You may not share your account credentials or make your account available to anyone else, and are responsible for all activities that occur under your account."
- **Acceptable Use Policy:** it prohibits accessing the Services "through unauthorized automated or non-human means, whether through a bot, script, or otherwise," and lists "reselling" among prohibited uses.

Without grants, the bridge stays inside those lines. Every member reaches only their own account, through xAI's authorized interfaces (automation webhook triggers and custom MCP connectors), and nobody pays for access.

Sources (as quoted by search on 2026-09-28; the pages block automated readers, so re-read them directly before relying on this):
- [xAI Terms of Service – Consumer](https://x.ai/legal/terms-of-service)
- [xAI Acceptable Use Policy](https://x.ai/legal/acceptable-use-policy)
- [xAI Consumer FAQs](https://x.ai/legal/faq) and [Privacy Policy](https://x.ai/legal/privacy-policy)
- [Discord Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy): no training AI models on message content obtained through the API. The bridge trains nothing; it forwards a member's own prompt to their own account at their request.

## Options

### Offer grants behind a flag (revisions 3.2–3.6)

An owner shares their automation with a Discord role, with an owner warning, a first-use confirmation for each grantee, and routing that refuses when two owners' grants apply. Even off by default, the feature exists to make one member's account available to others, which the consumer terms prohibit. It also carries a confused-deputy risk the bridge can't limit: a grantee's prompt runs with the owner's connectors.

### Don't support grants

Each member reaches only their own grok.com account. `ROLE_GRANTS_ENABLED=true` refuses to start, and `/grant` and `/ungrant` are not registered.

## Decision

Don't support role grants. Their purpose is to make one member's account available to others, which xAI's consumer terms prohibit, so no setting or safeguard makes them acceptable.

## Consequences

- Each member can reach only their own grok.com account, and the confused-deputy risk is gone.
- `ROLE_GRANTS_ENABLED=true` fails startup with a message citing xAI's consumer terms, and `scripts/register_commands.py` doesn't register `/grant` or `/ungrant`.
- Grant routing goes away with this decision.
- The grant sections of DESIGN.md, the role-grant PR, and the `grants` and `grant_acks` tables are kept only as a record. Removing the tables is a separate change, since `osctl` and the loopback API still list and delete grants.
- Members who want a shared assistant can't have one through the bridge. Each person connects their own account.

## Revisit if

xAI's consumer terms come to allow making an account available to others, or xAI offers an authorized way to share one. Re-read the sources above first, since this decision rests on them.
