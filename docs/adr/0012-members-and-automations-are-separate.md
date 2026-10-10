# ADR-0012: Members and automations are separate

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-29 |
| Merged | 2026-10-06 |
| Linear | MALU-209 (part of MALU-199; moved from DESIGN.md, Key decision 15) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 15) in MALU-199. The decision was made in revision 3.9 of the design, on 2026-09-29 (DESIGN.md's Date row; the commit, 6054b97 in #17, is stamped early on 2026-09-30), and built in PRs 8b and 8c (#18, #24, #25). |

## Context

Until then one registration row held both the member's identity and their webhook. That had two visible effects:
- **Connecting grok.com required a webhook.** In revision 3.8 an unregistered member who clicked Connect had to paste the trigger URL and secret before the connector could finish (built in #15, closed unmerged).
- **Removing a webhook removed the member.** In the pilot of PR 8a (#14), `/revoke` was used to try to disconnect only the connector, and it deleted everything.

The owner also wants a plugin direction, where a member's `/ask` can be answered by something other than a grok.com webhook.

## Options

### One registration row holding identity and webhook (revisions 2–3.8)

No schema change. Keeps both problems above, and every future kind of automation would add columns to the member row.

### Separate members and automations

A member is a Discord user the bridge knows: identity, `/settings`, and connector connections, and nothing that can start a run. An automation, with a `kind` (today only `grok_webhook`), is what answers `/ask`; it is set, replaced or removed on its own. `/leave`, with a required `confirm`, replaces `/revoke`.

## Decision

Separate members and automations. Connecting grok.com and setting a webhook become independent, removing a webhook keeps the member, and the automation `kind` is the seam for plugins.

## Consequences

- A member without an automation can't reach any grok.com account; `/ask` tells them to run `/webhook set`.
- Setting or replacing an automation removes the old one and inserts a new row, never updating a URL or secret in place, and withdraws that member's pending requests, even when the values are unchanged. Interactions record `automation_id`, and the re-checks require that same automation to still be active.
- The connector's tokens stay bound to the member, not the automation, so replacing a webhook never disconnects grok.com.
- The member record keeps the `registrations` table name, because codes, tokens, clients and interactions all reference `registration_id`.
- Command names changed in PR 8c-2 (#25: `/webhook`, `/leave`), so commands were re-registered once, and a stale `/revoke` answers "renamed".
- Joining costs only a Discord login, so empty members can fill `MAX_MEMBERS`; accepted in revision 3.9, with `USER_ALLOWLIST`, `SELF_SERVE=false` and the cap as the operator's levers.
- Revision 3.9 only draws the plugin seam. Plugin loading, installation and capabilities need their own design.

## Revisit if

- A second automation kind is designed, which may need more than one active automation per member (the table allows it; a partial unique index enforces one today).
