# ADR-0008: The server never holds the bot token

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-207 (part of MALU-199; moved from DESIGN.md, Key decision 10) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 10) and Alternatives considered (item 4) in MALU-199. The rule was in the first draft of the design, on 2026-09-27. |

## Context

Facts this depends on (Discord docs retrieved 2026-09-27):
- Editing the deferred response (`PATCH …/messages/@original`) and posting followups need only the interaction token, and don't depend on the bot's channel permissions.
- The bot token can call `PATCH /applications/@me` and move `interactions_endpoint_url`, so whoever holds it can take over the app's inbound traffic.
- Registering slash commands needs the bot token, but only when commands change.

## Options

### Use the bot token for replies, or Create Message

The bridge could post anywhere the bot can, at any time. The serving process would then hold a credential that can move the app's endpoint, and a compromise of the bridge or its host would give that away too.

### A Gateway bot using Create Message

The server holds the bot token and runs a supervised websocket. Free-form chat would also need the privileged Message Content intent.

### Interaction token only

Every Discord write the bridge makes uses the interaction token of the request it answers. The bot token is used only by `scripts/register_commands.py`, from a separate shell.

## Decision

Interaction token only. Everything the bridge has to write is an answer to an interaction, which the interaction token covers, so the bot token would only add a credential worth stealing.

## Consequences

- Every Discord write uses the interaction token. The bridge can write only in answer to an interaction, and only for 15 minutes after it ([ADR-0001](0001-replies-go-through-the-bridge.md)).
- Startup fails if `DISCORD_BOT_TOKEN` is set, at any value, and a test fails if the server package contains `Authorization: Bot`.
- A compromised bridge or host doesn't yield the bot token. What a stolen bot token yields depends on what passes through Discord, which this decision doesn't settle.
- Registering commands is a separate step, run from a shell that has the token, never from the server's environment.
- The bridge can't read Discord or start a conversation. Any future tool that does needs its own design for where the bot token lives.
- **Where the registering tool gets the token** (added 2026-10-07): only from `DISCORD_BOT_TOKEN` in the shell that runs `osctl command` (`scripts/register_commands.py` above, since renamed `scripts/osctl_command.py`), never from a file. Rejected, by the owner:
  - **a `0600` token file:** it sits on disk between runs, readable by anything running as the developer and copied into backups and synced folders, and on Windows its mode can't be checked. The owner chose it first, then withdrew it before merge.
  - **a `--token` argument:** it shows in the process list and shell history.
  - **a hidden prompt when the shell has none:** nothing on disk, but a new input path the owner judged not worth designing and testing.
- **The token never enters the container.** `osctl command` always runs where it's typed, never through `podman exec`. The registering code isn't in the image (`.dockerignore`), and doesn't share a file with the code that builds the container's environment. The tool that runs the container refuses an env file that names `DISCORD_BOT_TOKEN` in any case, or has a `*` in a key, which Podman would fill from the shell. `podman run` gets the env file, not the shell's environment, while Podman's `env_host` is off, its default (`--env-host=false` is passed where the client accepts it), and `podman exec` adds no host environment.
- **The token shares a process only with tool code that isn't called.** `osctl` imports just the module the chosen group needs, so a `command` run loads the registering code and never the container tool. The admin client code in `osctl.py` is loaded but not called on that path, and never reads the registration bearer there. Accepted; if `osctl.py` starts doing work at import or on the `command` path, `command` should run in a child process.

## Revisit if

A planned feature needs Discord access that the interaction token can't give, such as @mention chat over the Gateway or a tool that reads channels.
