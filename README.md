# Outer Steppes

> In the Diablo universe, the Outer Steppes is the passage leading out of the Pandemonium Fortress, the last bastion of the Light, into the heart of the Eternal Conflict.

Outer Steppes is a bridge between Discord and consumer AI platforms. Most AI bots call a developer API. Outer Steppes instead sends each question to the member's **own** grok.com account, so answers can use the connectors they already have there: mail, drive, Linear, GitHub, and anything else they've connected.

The bridge was designed with security and privacy in mind. It never stores or logs prompt or answer text, and never logs tokens or secrets. It stores only each member's Discord id, settings, webhook and connector tokens, plus request records that are deleted 24 hours after they finish.

## Design highlights

- **The prompt never travels in the webhook.** The bridge can't prove that a webhook URL belongs to the member who saved it, so the POST carries only a single-use reply token. The prompt and the answer move only through the member's own OAuth-bound connector, which is what proves whose Grok is running.
- **No AI keys, and no bot token in the server.** The bridge never generates text or holds an API key. The Discord bot token is used only from a separate shell to register commands, and the server refuses to start if it's in its environment.
- **Secrets are stored by how they're used.** Webhook secrets the bridge must sign with are kept; tokens it only checks are stored as hashes.
- **Personal by default.** A new bridge accepts nobody until its owner [claims it with a one-time link](docs/adr/0016-personal-by-default-claimed-through-a-link.md), and then membership is locked.
- **No open ports.** TLS ends on the owner's machine through Tailscale Funnel; the admin API listens on loopback only.

Each decision, and the alternatives it rejected, is recorded in [`docs/adr/`](docs/adr/README.md). The threat model is in [DESIGN.md](DESIGN.md#threat-model).

## Features

- **Commands:** `/ask` (with `private`), `/settings`, `/status`, `/webhook set|status|remove`, `/leave`.
- **Install anywhere:** to a server or to your own Discord account. Answers are private by default; `/settings` and `/ask private:` change that.
- **Automations:** a grok.com automation webhook (`whsec_` secret) or a Grok Bot routine webhook (`crsr_` key). Members set their own on the bridge's account page, `https://<host>/account`.
- **MCP connector:** grok.com connects over OAuth, through Discord login or a per-member client. `/ask` is refused until it's connected.
- **Safety rails:** verified interactions, allowlists, rate limits, a kill switch (`osctl bridge pause`), and replies that expire after 14 minutes.
- **Operations:** a loopback-only admin API, Prometheus metrics, JSON logs, hourly data retention, and a non-root container behind Tailscale Funnel.

## How it works

```
Discord member ──/ask──▶ Discord ──signed interaction──▶ Outer Steppes
                                                              │ signed webhook POST (a reply token only)
                                                              ▼
                                         the member's grok.com automation
                                    (their Grok, their connectors, their history)
                                                              │ prompt and answer, through the member's
                                                              ▼ Outer Steppes connector: get_prompt, discord_reply
Discord member ◀──edited reply──  Discord  ◀─────────────── Outer Steppes
```

1. A member runs `/ask`, in a server or anywhere they've installed the app, DMs included. The bridge verifies Discord's signature and defers the reply.
2. The bridge POSTs to the member's grok.com automation trigger. The POST carries only a **single-use reply token**, never the prompt.
3. The automation fetches the prompt with the bridge's MCP tool `get_prompt` and answers with `discord_reply`, both through the member's own Outer Steppes connector.
4. The bridge edits the Discord message into the answer, without pinging anyone.


## Privacy Disclaimer

**Whoever runs a bridge can use its members' grok.com automations, and the connectors they run with.** Later implementations aim to provide secure multi-user support. For now, the operator of the bridge has unrestricted access to all data stored on and traveling through the bridge including keys and message data, so joining someone else's bridge is highly discouraged.

Instead, the recommended setup is to **run your own bridge, on a machine you control, for yourself**. That's the default: nobody can join a new bridge until you claim it with a one-time link, and then membership is locked. Run one for others only if they'd trust you with their grok.com account, and only join someone else's if you'd trust them with yours ([threat model](DESIGN.md#threat-model)).

## Set up a bridge

You need Python 3.12+, Podman, and Tailscale with Funnel on and the machine name pinned ([Deployment](#deployment)). `<host>` below is `<machine>.<tailnet>.ts.net`.

1. **Discord Developer Portal:** create an application. Under **Installation**, enable User Install, Guild Install, or both, with the scope `applications.commands`. Under **OAuth2**, add the redirect `https://<host>/oauth/discord/callback` and reset the secret. Under **Bot**, turn **Public Bot** off.
2. **Env file (.env):**
   ```
   DISCORD_PUBLIC_KEY=<General Information → Public Key>
   DISCORD_APPLICATION_ID=<General Information → Application ID>
   DISCORD_CLIENT_SECRET=<OAuth2 → Client Secret>
   BRIDGE_REGISTRATION_TOKEN=<32+ random characters>
   PUBLIC_BASE_URL=https://<host>
   ```
3. **Start it:**
   ```powershell
   python -m venv .venv; .\.venv\Scripts\pip install -e .
   .\.venv\Scripts\activate
   $env:OS_ENV_FILE = "<path to the env file>"
   osctl container up --build    # prints a one-time claim link: keep it private
   tailscale funnel --bg 127.0.0.1:8080
   ```
4. **Claim it:** open the claim link, sign in with Discord, and follow the account page's steps: create a grok.com automation, save its webhook, add the connector, install the app.
5. **Discord:** set the Interactions Endpoint URL (General Information) to `https://<host>/interactions`. Then, in a new shell with `DISCORD_BOT_TOKEN` and `OS_ENV_FILE` set, run `osctl command register`.


Then run `/ask`.

## Sharing a bridge

`MEMBERSHIP` decides who may join once the bridge is claimed:

| Value | Who joins |
| --- | --- |
| `personal` (default with Discord login) | Nobody else, unless you run `osctl member add` |
| `shared` | Any Discord user who signs in on the account page or connects the connector |
| `operator` (default without Discord login) | Only members you add; there's no claim link |

`USER_ALLOWLIST` and `MAX_MEMBERS` narrow it further, and `MEMBER_IDLE_DAYS` removes idle members ([DESIGN.md → Members and automations](DESIGN.md#members-and-automations)). After changing any of them, recreate the container (`osctl container restart`).

Invite only people who trust you with their grok.com account. Send them the account page's address, `https://<host>/account`, and ask them to **bookmark it and check the host before pasting a webhook and whsec_**. On a shared bridge they sign in there, which makes them a member, and follow the page's steps. Otherwise you add them with `osctl member add <discord user id>`, which prompts for their automation's trigger URL and secret. The account page also lets members remove their automation or leave.


## Deployment

```powershell
# osctl container is a simple wrapper for frequent podman commands
> osctl container --help
usage: osctl container [-h] {build,up,restart,down,status,logs,exec,tunnel,db-snapshot,reset,restore} ...
...
positional arguments:
  {build,up,restart,down,status,logs,exec,tunnel,db-snapshot,reset,restore}
    exec                CMD... inside the container (default: sh)
    reset               delete the data volume and start on an empty database
    restore             replace the data volume with a snapshot and start on it
    
# osctl container tunnel runs Tailscale Funnel to 127.0.0.1:8080 until Ctrl-C
> osctl container tunnel --help
usage: osctl container tunnel [-h] [--url URL] [--env-file ENV_FILE]
...
options:
  -h, --help           show this help message and exit
  --url URL            defaults to PUBLIC_BASE_URL from the env file
  --env-file ENV_FILE  defaults to OS_ENV_FILE, then .env)
```


## Development

```powershell
# create and activate the python virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# install test and dev packages
pip install -e ".[test,dev]"
pre-commit install      # gitleaks on every commit
python -m pytest -q
```

The integration tests (`tests/integration/`) boot the real service with only Discord and grok.com faked. Skip them with `-m "not integration"` for a faster loop.

## Documentation

- **[DESIGN.md](DESIGN.md):** what the system does: trust boundaries, the request path, OAuth, the threat model.
- **[docs/adr/](docs/adr/README.md):** why: one record per architecture decision.
- **[SECURITY.md](SECURITY.md):** reporting a vulnerability.
- **[docs/outer-steppes.excalidraw](docs/outer-steppes.excalidraw):** an end-to-end diagram; open it at excalidraw.com.

## How this was built

I built Outer Steppes with AI coding agents (mainly Claude Code) doing most of the implementation. My part was the system itself: the requirements, the trust model, and every architecture decision, including the ones recorded after a review or a pilot proved an earlier design wrong. [ADR-0017](docs/adr/0017-prompts-and-answers-through-the-connector.md), which moved the prompt out of the webhook, is one of those.

The process is built so that no change gets in without evidence:

- **Design before code.** A behavior change starts as an edit to DESIGN.md, and an ADR when it's an architecture decision, reviewed before any code is written.
- **Deliberate breaks.** For risky code, each safety behavior is broken on purpose, one at a time, to prove a test catches it.
- **Independent review.** Every branch gets a separate review pass before it's pushed, and findings are fixed or answered in writing.
- **Owner merge.** I review every PR and its risk notes, and only I merge.

## Status

Built and piloted end to end in Discord, with real grok.com automations and a Grok Bot routine answering through the connector. There's no release yet.

## License

Copyright (C) 2026 Adam Butac.

Licensed under the [GNU Affero General Public License v3.0 only](LICENSE) (`AGPL-3.0-only`). If you run a modified version as a service for other people, you must offer them its source code. It comes with no warranty.

## Terms

Each member uses **only their own** grok.com account, through xAI's own interfaces (automation webhooks and custom MCP connectors), and nobody pays for access through the bridge. [Sharing one automation with others isn't supported](docs/adr/0009-no-role-grants.md): xAI's consumer terms say you **may not "make your account available to anyone else"**, and the acceptable-use policy prohibits "unauthorized automated" access and reselling.

Re-read the sources yourself; this isn't legal advice: [xAI Terms of Service – Consumer](https://x.ai/legal/terms-of-service), [Acceptable Use Policy](https://x.ai/legal/acceptable-use-policy), [Consumer FAQs](https://x.ai/legal/faq), [Privacy Policy](https://x.ai/legal/privacy-policy), [Discord Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy).

Answers are Grok's output and are attributed to Grok, per xAI's guidance. The bridge isn't affiliated with xAI, and a Discord app shouldn't be named "Grok".
