# ADR-0017: Prompts and answers pass only through the member's connector

| Field | Value |
| --- | --- |
| Status | Partly superseded by [ADR-0018](0018-another-members-connector-bans-the-signing-secret.md): how a run that calls through another member's connector ends: with `unknown` and a timeout, a run per `/ask` until the member gives up |
| Opened | 2026-10-07 |
| Merged | 2026-10-08 |
| Linear | MALU-214 |
| Supersedes | [ADR-0001](0001-replies-go-through-the-bridge.md), the HTTP `/reply` path and that a reply token alone can post; [ADR-0003](0003-interaction-tokens-are-stored.md), its reason for keeping prompts out of the database (they still stay out), its stored prompt excerpt, and that `posted` replies always survive a restart |

## Context

A member's automation is a grok.com trigger URL and its `whsec_` secret. For each accepted `/ask`, the bridge signs one POST to the asking member's trigger. Until this decision the POST carried the prompt and a single-use reply token, and the run handed its answer back with that token: by POSTing it to `/reply`, or by calling the `discord_reply` tool on the bridge's MCP server, which grok.com reaches as a custom connector.

Facts this depends on, as of this date:
- **A trigger belongs to the member who saved it.** It's saved under the Discord user signed in on the account page, or under the member the operator saved it for. `/ask` fires only the asker's own trigger, and the interaction and its reply token are recorded against that member.
- **A connector belongs to a member.** Its OAuth tokens belong to the Discord user who signed in when the connector was added ([ADR-0011](0011-discord-login-ties-the-connector-to-a-member.md)), or, with an issued client, to the member the operator issued it for. Every tool call through it carries that member's access token, and the bridge can compare it with the interaction's member.
- **Nothing else identifies the run's account.** Nothing in a trigger URL, the signed POST or a run's outbound HTTP names a grok.com account. The `whsec_` secret proves only that the sender knows it, and the member who saved the trigger does. Saving checks only the URL's shape, the host's DNS and the secret's format, so a member can save a trigger from someone else's account.
- **The run sees the whole POST body.** Whatever account owns the trigger gets the prompt and the reply token, and its run follows the prompt. A member who controls the prompt could tell a foreign run to send the reply token out, then deliver it with their own access token, which they can obtain outside grok.com.
- **Holding a trigger is already power over its account.** Whoever has a trigger's URL and secret can start runs there without the bridge.
- **The connector works in runs:** pilots have delivered real answers through it from automation runs, and it's what every member is told to set up. ADR-0001 kept HTTP because MCP rests entirely on the connector, while HTTP needed only outbound HTTP.
- **Only an excerpt of each prompt is stored** ([ADR-0003](0003-interaction-tokens-are-stored.md)): 200 characters, one line, for the repeat check and the quote above an answer; the worker holds the prompt in memory, and the bridge runs as one replica. ADR-0003 stores interaction tokens because a restart that dropped them left members on "thinking…" with no way to tell them. Its reason for keeping prompts out, that they aren't needed after the POST, stops holding under this decision.
- **Unobserved:** whether models reliably call a fetch tool before answering, and whether they act on a prompt that arrives as tool output as fully as on one in the trigger body.

## Options

### Keep both paths, prompt in the POST

No change. A foreign trigger's account receives every prompt, and its answers are delivered over HTTP.

### MCP-only answers, prompt in the POST

Retire `/reply`. A reply token alone can't post, and a foreign trigger saved by mistake never gets an answer delivered. But the foreign account still receives every prompt, and a member who deliberately uses someone else's trigger can relay the reply token out through their prompt.

### MCP-only answers, plus a connection check at save time

As above, plus one fixed run after each save, with `/ask` refused until it answers over MCP from the member's connector. The foreign account gets only fixed runs until the check passes. It costs a new state on every automation, a retry action, more endings to handle (pause, removal, restart, expiry) and a run per save, and it protects nothing after a check passes.

### Prompt by reference: both prompt and answer through the connector

Retire `/reply`, and take the prompt, the caller's id and the channel out of the POST, which carries only the reply token and its expiry. The run fetches the prompt with a `get_prompt` tool, through its connector, and the bridge hands it over only if the connector belongs to the asking member. A foreign trigger's account then receives an opaque token and nothing else: no prompt, and nothing in the run for a member to steer, so the relay above is closed too. It costs a second tool call per run, a dependency on the model calling `get_prompt` first, and an MCP tool that returns prompt text. The prompt now has to outlive the POST. Stored with the row while the ask is live and cleared at its end, asks would survive a restart, but any snapshot taken meanwhile would hold the prompt. Held in memory, with the excerpt replaced by a keyed MAC for the repeat check and the quote taken from memory, no prompt text is ever at rest: prompt text is the most sensitive thing a member sends, and anything in the database persists, snapshots included. That costs asks not yet fetched at a restart, ended with a message to ask again, and the quote on an answer that lands after one.

## Decision

Prompt by reference, with prompts held only in memory and no excerpt stored (owner, 2026-10-08, building on the owner's MCP-only decision of 2026-10-07). It's the only option that keeps a member's prompt, and the answer to it, out of any grok.com account that doesn't hold that member's connector. The bridge enforces it at fetch time, so it doesn't depend on a foreign automation behaving, and it covers every `/ask`, not only a check at save time.

## Consequences

- Every automation needs the Outer Steppes connector, connected as the same member, in the trigger's grok.com account. A bridge with Discord login off needs an issued client for each member.
- An access token alone reads and posts nothing, and a reply token alone does neither: both tools need the token and the same member's access token.
- A foreign trigger's account gets one short run per `/ask` with an opaque token, until the member gives up. Its owner sees no prompt and can't get an answer delivered, unless that owner deliberately cooperates with the member.
- The bridge still can't tell who owns a grok.com account; it proves only that the run's account holds the asking member's connector.
- No prompt text is stored, not even the excerpt: the repeat check compares an HMAC under a random key made at each startup, and the bridge holds each prompt in memory until its interaction ends. An answer that lands after a restart posts without its quote. A restart drops the held prompts; a run that fetches afterwards ends the ask with a message to ask again, using the stored interaction token, so the restart doesn't strand members the way ADR-0003 avoids. A run that never fetches times out as before.
- Each run needs two tool calls. If models skip `get_prompt`, runs answer nothing useful (Revisit if).
- Setup mistakes show as timeouts, not at save time.
- Whether the member holds a live connection can decide `/ask` for every member, so the bridge no longer records which path a member's answers take.
- Connecting grok.com and setting an automation are still stored and changed separately, in either order; `/ask` works only once both are done. [ADR-0012](0012-members-and-automations-are-separate.md) decided the storage, not the reply path, so it isn't superseded.
- One grok.com account serves one member. grok.com refuses a second connector with the same name in one account, and a connector under another name has other tool names, which the instructions don't call (observed while building, 2026-10-08). A run that reaches another member's connector gets `unknown`, and the request times out; nothing is read or posted from the wrong place.
- There is no HTTP reply path, and the payload carries no prompt, Discord ids or `reply_url`. DESIGN.md gains `get_prompt` (its rules, the MCP tool, a new state-machine ending and a new trigger for an existing one and a metric), new automation instructions, a smaller payload and test vector, and loses `/reply` and the per-member record of the reply path; README.md, SECURITY.md and the full diagram change with the code.

## Revisit if

- Models don't reliably call `get_prompt` before answering, or treat a fetched prompt as less trustworthy than one in the trigger body.
- grok.com runs can no longer call custom connectors, or connector tool naming changes in a way automations can't follow.
- grok.com starts identifying the account behind a trigger or a run's requests.
- Foreign triggers or setup mistakes turn out common enough that members need to learn about them at save time.
- Members need to share one grok.com account.
