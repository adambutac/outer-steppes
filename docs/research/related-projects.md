# Related projects and possible duplicate efforts

| Field | Value |
| --- | --- |
| Date | First pass 2026-09-29; [second pass](#second-pass-2026-10-06) 2026-10-06 |
| Status | Two passes. Neither found a direct duplicate, and neither changed what the design decides (this pass only adds a pointer to this note in DESIGN.md). The Grok Bot findings feed MALU-43 |
| Method | First pass: four web searches, then fetched READMEs and one forum thread. Second pass: searches beyond GitHub (vendor sites and help pages, the Discord App Directory, Cursor's forum, guides), GitHub API metadata for each close project, READMEs, and the code of the four closest projects ([Checked in code](#checked-in-code)). Claims marked as checked in code were confirmed there; the rest come from READMEs or summaries of pages, so verify them before relying on them |

## Question

Is anyone else building what Outer Steppes does? That means a Discord front end where each member uses **their own consumer AI account**, reached only through the platform's own features (automation triggers and a connector), with the bridge holding no bot token and no shared account.

## Findings

### 1. Discord bridges to "Grok Bot" (closest in shape)

- **[RudeDude/grokbot-discord](https://github.com/RudeDude/grokbot-discord)** (MIT, very small):
  - **Architecture close to ours:** a Discord message becomes a POST to an automation "routine" webhook, and the agent calls back to the bridge's `/reply` with a shared `CALLBACK_SECRET`. The bridge then posts to Discord, acknowledging with "Got it." first.
  - **Differences:** a single operator with YAML config (`bots.yaml`, "one Discord bot, many Grok Bots"); the Discord Gateway with a bot token; message and alias parsing instead of slash commands; guild, channel, and author allowlists.
- **[davefmurray/grok-bot-discord](https://github.com/davefmurray/grok-bot-discord)** (MIT, very small): a Gateway bridge with fail-closed guild, channel, and author allowlists and a switchable "brain" (`openai_compat` to xAI's API, a generic `webhook`, or `log_only`). Its README says there was no first-party Discord inbound plugin as of mid-August 2026.
- **discord-agent-bridge** ([FlyOverCoderKY/discord-agent-bridge](https://github.com/FlyOverCoderKY/discord-agent-bridge), identified in the [second pass](#close-projects-with-activity)), mentioned in a [Cursor forum request for an official Discord plugin](https://forum.cursor.com/t/official-discord-plugin-for-grok-bot-inbound-channels/168430). A vendor reply: "makes sense… no ETA to share."
- **Resolved since (MALU-43):** "Grok Bot" is xAI's agent product, separate from grok.com's consumer automations. Cursor sells it too, which is why the request is on Cursor's forum. Sources: [xAI's product page](https://x.ai/bot) and the [Grok Bot Terms](https://cursor.com/terms/grok-bot) on Cursor's site.

### 2. MCP servers that let an AI control Discord (the opposite direction)

- **[SaseQ/discord-mcp](https://github.com/SaseQ/discord-mcp)**, **[iprashantraj/mcp-discord-bridge](https://github.com/iprashantraj/mcp-discord-bridge)**, **[Quads Lab Discord MCP](https://quadslab.io/products/discord-mcp)** (134 tools), and a Claude Code "Discord Bridge" MCP server with tools including `discord_reply`.
- They give an AI broad, bot-token-level power: reading, posting anywhere, managing servers. Outer Steppes's single MCP tool is the reverse: it can only answer the one `/ask` whose reply token it holds.

### 3. Multi-provider Discord AI bots (a shared operator account)

- **[llmcord](https://github.com/Oleg4260/llmcord)**, **[OpenLLM](https://github.com/OpenLLMteam/openllm)**, **[OoriData/Discord-AI-Agent](https://github.com/OoriData/Discord-AI-Agent)** (MCP), and **[discord-multi-ai-bot](https://github.com/binkiewka/discord-multi-ai-bot)**.
- All run on the operator's API keys, so everyone shares one account. That model is what [ADR-0009](../adr/0009-no-role-grants.md) rules out under xAI's consumer terms.
- **OpenLLM** has a plugin system whose manifests declare **permissions** (for example `["network", "storage"]`), with per-server permission controls and "sandboxing". This is relevant to the planned plugin model.

### 4. Bring-your-own-key, user-installable bots

- **[VibeBot](https://www.vibebot.gg/)**, OpenClaw Easy, and a [user-installable "portable AI companion" tutorial](https://dev.to/waveplay/build-a-portable-ai-companion-with-discords-user-installable-apps-gpt-4o-5f20).
- Members paste an **API key** into the bot, so the bot's operator holds members' credentials. Outer Steppes holds no AI credential; it holds only a webhook secret that starts one automation.

## Not found

No project combining all of these:
- a per-member **consumer** account, reached through the platform's own automation and connector features
- the Interactions Endpoint with **no bot token in the server**
- Discord-signed identity plus Discord login
- webhook secrets that never pass through Discord

[No official xAI Discord bot](https://www.layer3labs.io/guides/grok-discord-bot) was found either.

## Ideas noted, not adopted

These are recorded for the planned plugin design. None are decided.

1. **Plugin manifests with declared capabilities**, in the spirit of OpenLLM's, but enforced. For example, the grok.com plugin would declare one outbound host, signed webhooks, and one reply tool, much like `url_policy` pins grok.com today.
2. **"Grok Bot" routines as a possible second plugin.** RudeDude's bridge shows the same webhook-plus-callback pattern. This depends on that product's terms; the second pass reads them in [Terms of a plugin target](#terms-of-a-plugin-target-grok-bot).
3. **A narrow MCP tool as a core rule:** plugins never get broad Discord powers, and every tool is scoped to a reply token.
4. **Several automations per member** ("one Discord bot, many Grok Bots"): allow it in the data model, ship one.

## Second pass (2026-10-06)

Tracked in Linear as MALU-31. It covered the four follow-ups the first pass listed: search beyond GitHub, settle who makes "Grok Bot" (done by MALU-43), check whether any close project is a direct duplicate, and review the terms of a plugin target.

### Verdict: no direct duplicate

Every close project has the same shape:
- **Bot token.** It holds a Discord bot token and listens on the Gateway.
- **The operator's accounts.** It reaches only the operator's AI account or accounts, never a member's own.
- **Shared use.** Anyone the operator allowlists can drive that account.

None gives each member their own consumer account through the platform's own trigger and connector, and none runs without a bot token. The first pass's [Not found](#not-found) list still holds.

### Close projects, with activity

GitHub data as of 2026-10-06. "Pushed" is GitHub's last push to any branch. "Code" marks the four projects whose code was read (see [Checked in code](#checked-in-code)); the others rest on their READMEs or page summaries.

| Project | Created | Pushed | Stars | Code | Shape | Why it isn't a duplicate |
| --- | --- | --- | --- | --- | --- | --- |
| [FlyOverCoderKY/discord-agent-bridge](https://github.com/FlyOverCoderKY/discord-agent-bridge) | 2026-08-12 | 2026-08-30 (last commit on `main`, its only branch: 2026-08-14) | 2 | Yes | Gateway bridge. Sends `{ agentId, prompt }` to an undocumented `sendPrompt` gateway on the Grok Bot's computer, and gets the answer back on `/internal/reply` | One `GROK_BOT_AGENT_ID` serves every allowlisted user (users, roles, or everyone). Undocumented inbound path |
| [davefmurray/grok-bot-discord](https://github.com/davefmurray/grok-bot-discord) | 2026-08-17 | 2026-10-04 | 1 | No | Gateway bridge built for rooms where bots talk to each other (Grok Bot and Hermes). Its "brain" options now include `gbot`, which uses a community `grok-bot-cli` | One operator, bot token. Its `docs/COMPARISON.md` compares it with discord-agent-bridge |
| [RudeDude/grokbot-discord](https://github.com/RudeDude/grokbot-discord) | 2026-08-29 | 2026-08-29 | 0 | Yes | Gateway bridge. Calls a routine's documented webhook (Bearer key) and gets a `/reply` callback. One Discord bot fronts several Grok Bots, each with its own webhook URL and key | Routing goes by alias or role, never by author: any allowlisted author can wake any configured Grok Bot. No pushes since the first day |
| [matthew-rutledge-dev/grok-bot-discord-bridge](https://github.com/matthew-rutledge-dev/grok-bot-discord-bridge) | 2026-09-29 | 2026-10-05 | 0 | Yes | Gateway bridge. Calls the same `sendPrompt` gateway as discord-agent-bridge, maps channels to agents, refuses by default, and pairs DMs. Its companion [grokbot-discord-fleet](https://github.com/matthew-rutledge-dev/grokbot-discord-fleet) is a Cursor plugin with Discord status and moderation tools over MCP | One `GROK_BOT_GATEWAY_TOKEN`, so one Grok Bot computer, for every allowlisted user and role. The most active Grok Bot bridge |
| [NinjaProtocol/grokbot-discord-plugin](https://github.com/NinjaProtocol/grokbot-discord-plugin) | 2026-08-26 | 2026-08-26 | 1 | No | Gateway bot that answers @mentions in chosen channels and relays them to the operator's local Grok Bot | One operator, bot token |
| [Zero6992/chatGPT-discord-bot](https://github.com/Zero6992/chatGPT-discord-bot) | 2022-12-04 | 2026-09-12 | 2.7k | Yes | Multi-provider slash-command bot on the Gateway. It can also run the Codex, Claude Code, or Grok Build CLI on an account login | One login per configured CLI backend, made by that backend's `owner_id`. Every allowed user's request runs on that login; only conversation state is kept per user. That's the same kind of sharing [ADR-0009](../adr/0009-no-role-grants.md) rules out for this bridge |
| [angdulu/discord-ai-team](https://github.com/angdulu/discord-ai-team) | 2026-09-24 | 2026-10-04 | 0 | No | Claude, Codex, and Gemini official CLIs on the owner's machine, signed in with the owner's subscriptions, one bot token per agent | One owner's accounts. Its README warns: "Don't use it to offer your subscription to other people", the same line ADR-0009 draws |

### Checked in code

Shallow clones of the default branch, read on 2026-10-06; nothing was run. Commits: discord-agent-bridge `3657468`, grokbot-discord `7d096fc`, grok-bot-discord-bridge `2cd4484`, chatGPT-discord-bot `213347d`.

- **Every account is the operator's.** Every AI-side credential is the operator's: one Grok Bot agent id (discord-agent-bridge, `src/config.ts`), one gateway token (grok-bot-discord-bridge, `src/config.ts`), webhook keys the operator configures (grokbot-discord, `bots.example.yaml`), or one CLI login volume per backend (chatGPT-discord-bot, `AccountProfile` in `src/cli_accounts.py`, mounted for every conversation in `src/cli.py`). No project lets a member bring their own.
- **Others drive that account.** All four let allowlisted Discord users use it. discord-agent-bridge and grok-bot-discord-bridge can allowlist roles too. grokbot-discord uses a role only to address a bot, not to allow the sender. discord-agent-bridge also has an allow-everyone switch, and chatGPT-discord-bot allows everyone when `allowed_user_ids` is empty, its default. It's the same kind of sharing as the role grants Outer Steppes retired for itself under [ADR-0009](../adr/0009-no-role-grants.md).
- **The reply callback.** Each Grok Bot bridge gives the agent one long-lived shared secret for its callback, not a single-use token for each request. Outer Steppes's reply token is single-use and scoped to one `/ask`. This note stays at that design level on purpose: it records how other projects compare, not weaknesses in their code.
- **Inbound path.** Only grokbot-discord uses a routine's documented webhook. The other two Grok Bot bridges call the `sendPrompt` gateway, which discord-agent-bridge's own code calls "Unofficial".

Seen but not close:
- [ryelae/askgrok](https://github.com/ryelae/askgrok) drove X's web UI with Playwright and is abandoned.
- [Composio's Discord toolkit for Grok Bot](https://composio.dev/toolkits/discord/framework/grok-bot) gives the bot tools to act in Discord, the opposite direction, like the first pass's §2.
- [Hookdeck](https://hookdeck.com/webhooks/platforms/using-hookdeck-with-grok-bot-reliable-webhook-triggers) and several small repos use a routine's webhook to wake a Grok Bot from other sources. The pattern is common; none of them is about Discord.

### Beyond GitHub

- **xAI:** still no official Grok Discord bot. Two guides say so as of August 2026 ([Layer3 Labs](https://www.layer3labs.io/guides/grok-discord-bot), [Nicolás Torres](https://ntorres.dev/blog/grok-bot-guide)). The Discord App Directory has an app called "Grok", but its page returned only a title, so who publishes it isn't confirmed. The second guide names an official Telegram bot.
- **Cursor:** in the [forum thread](https://forum.cursor.com/t/official-discord-plugin-for-grok-bot-inbound-channels/168430), the only staff reply (2026-08-18) says an official Discord plugin "makes sense", with "no ETA to share". Discord's bot detection is one of the things to work out first.
- **The vendor's own answer is Slack, for teams.** Grok Team Bots (public beta, September 2026, Teams and Enterprise plans) give a team one shared bot with its own Slack handle and private chats for each user. That's a shared business account, which is the opposite of a member's own consumer account. Grok Bot routines can be triggered by Slack events, but [Cursor's routines help page](https://cursor.com/help/grok-bot/routines) doesn't mention Discord.
- **[Claude Code Channels](https://code.claude.com/docs/en/channels)** (Anthropic, research preview since 2026-03-20) connects one person's running Claude Code session to Discord through a plugin that holds that person's own bot token. It's the same one-owner shape as the GitHub bridges.

### What would change the picture

An official Discord inbound plugin for Grok Bot. It would cover what MALU-43 (support Grok Bots) is for, but not the grok.com automation path this bridge uses today. Recheck the forum thread before MALU-43's design pass.

### Terms of a plugin target: Grok Bot

Grok Bot is the only platform being considered as a plugin target (MALU-43). This is a reading for design, not legal advice.

- **Which terms.** The [Grok Bot Terms](https://cursor.com/terms/grok-bot) (Cursor's copy, last updated 2026-09-03) add to "the agreement governing Customer's use of the Services". That suggests a SuperGrok member is also bound by xAI's consumer terms, and a Cursor member by Cursor's. This is an inference from Cursor's copy only; MALU-43 should confirm it. xAI's own copy (`x.ai/legal/grok-bot-terms`) returned 403 to both fetch methods tried, so the two copies weren't compared.
- **Who may use it.** "Customer may permit its Users to access and use Grok Bot for Customer's internal business purposes", where a User is "an individual authorized to use Grok Bot". When the member is the customer, a bridge that triggers only that member's own bot keeps ADR-0009's position. Open for MALU-43: how "internal business purposes" reads for a member on a consumer plan using it from Discord.
- **Responsibility for actions.** The customer is "solely responsible for ensuring those Agentic Actions are authorized, lawful, and comply with all applicable laws and third party terms". A prompt sent from Discord is still the member's instruction to their own bot. The bridge should keep it that way: no other member's text should reach someone else's bot.
- **Shared environment.** "Bots associated with the same User may share that environment and must not be treated as separate security boundaries." A gateway on the bot's computer, the path discord-agent-bridge and grok-bot-discord-bridge use, sits next to the member's browser sessions and credentials. MALU-43's scope already flags this.
- **Documented inbound path.** A routine's webhook trigger is documented: a POST with `Authorization: Bearer <key>` and an optional JSON body that the bot receives together with the routine's instruction. The `sendPrompt` gateway isn't documented. This note recommends the webhook for MALU-43, in the spirit of [ADR-0009](../adr/0009-no-role-grants.md), which reaches xAI only through its authorized interfaces. That's this note's judgement, not a recorded decision; if it should bind plugins, it needs an ADR in the plugin design.

Related: [ADR-0020: Grok Bot routines are a second automation kind, authenticated with a bearer key](../adr/0020-grok-bot-webhooks-use-a-bearer-key.md)
