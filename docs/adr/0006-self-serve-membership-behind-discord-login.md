# ADR-0006: Membership is self-serve behind Discord login; webhook secrets go only to the bridge's page

| Field | Value |
| --- | --- |
| Status | Partly superseded by [ADR-0016](0016-personal-by-default-claimed-through-a-link.md): self-serve membership as the default |
| Opened | 2026-09-29 |
| Merged | 2026-10-06 |
| Linear | MALU-209 (part of MALU-199; moved from DESIGN.md, Key decision 7) |
| Supersedes | None |
| Origin | Key decision 7 and Alternatives considered item 8. This form is revision 3.9 (DESIGN.md's Date row; commit 6054b97 in #17 is stamped early on 2026-09-30). Earlier forms, kept below as options: revision 3's "Registration is operator-only" (2026-09-27, when the Key decisions table arrived), and revision 3.8's first self-serve registration (2026-09-29, #11). |

## Context

A member gives the bridge two secrets-in-effect: their grok.com automation's trigger URL and its `whsec_` signing secret. Together they start runs in the member's grok.com account. The question is how they reach the bridge, and who has to be involved.

Facts this depends on:
- A stolen bot token lets its holder move the app's Interactions Endpoint and read every later command, including option values and modal submissions. Anything a member types into Discord after that goes to the thief.
- The bridge already runs Discord OAuth2 (`identify`) for the grok.com connector ([ADR-0011](0011-discord-login-ties-the-connector-to-a-member.md)), so it can know which Discord user is in a browser without a new credential.

## Options

### Operator-only registration (the only path in revisions 2–3.7)

Members hand their URL and secret to the operator out of band, and the operator registers them on the loopback API. `whsec_` never passes through Discord, but every member needs a manual handoff, and the secret passes through whatever channel the two of them use.

### A Discord modal

A `/register` command opens a modal for the URL and secret. Easiest for members. Offered in the first draft (2026-09-27) beside the operator's API, dropped in revision 2, and rejected again in 3.9 for `/webhook set`, each time because `whsec_` would pass through Discord, and after a bot-token theft every later secret would be harvested without the member noticing.

### Registering inside the connector flow (revision 3.8, #15, closed)

An unregistered member clicking Connect in grok.com signs in with Discord, then pastes the URL and secret on a bridge form before the connector finishes. `whsec_` stays off Discord. Built in #15 and not merged: it tied connecting grok.com to having a webhook, and the owner proposed making the two independent ([ADR-0012](0012-members-and-automations-are-separate.md)). Its Save also redirected back to grok.com, which needed grok.com's origin in `form-action`.

### Self-serve, with the webhook set only on the bridge's account page (revision 3.9)

A verified Discord user becomes a member by connecting grok.com, and sets the webhook later on `/account`, behind the same Discord login; `/webhook set` replies with only a link there. The operator's loopback API stays.

## Decision

Self-serve membership behind Discord login, with the webhook set only on `/account`. It needs no operator for onboarding, and `whsec_` goes from the member's browser to the bridge over TLS, never through Discord.

## Consequences

- A stolen bot token can no longer harvest webhook secrets. It can still phish for them: the thief can answer `/webhook set` with a link to a look-alike page, and writes the reply's text too. The defence is members knowing the host (bookmarking `/account`, as the README says).
- Onboarding needs no operator. Any Discord user can join unless `USER_ALLOWLIST` or `MAX_MEMBERS` limits them, or `SELF_SERVE` is off. Whether a bridge should default to personal is an open question (MALU-231).
- The bridge gains a web surface with sessions, a CSRF token, `Origin` checks and rate limits, and it is part of the top risk tier.
- The bridge still can't prove that a trigger the member saves runs in their own grok.com account. The connector login does tie one grok.com account to the member, since that account holds MCP tokens bound to their membership ([ADR-0011](0011-discord-login-ties-the-connector-to-a-member.md)), and an answer over MCP is refused unless its token's member matches the interaction's. But a trigger URL names no account, and an answer over HTTP `/reply` carries only the reply token, so on that path nothing links the trigger to the connected account.
- The operator keeps the loopback API and `osctl`, which still register a member and their automation in one call.

## Revisit if

- Discord offers a way to collect a secret that doesn't pass through the app's Interactions Endpoint.
- Phishing through a stolen bot token is seen in practice, which would call for a stronger way for members to recognize the real page.
