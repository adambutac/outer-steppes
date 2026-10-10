# ADR-0019: The bridge sends the run's instructions in the trigger body

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-10-09 |
| Merged | 2026-10-09 |
| Linear | MALU-283 |
| Supersedes | None |

## Context

Each `/ask` signs one POST to the member's grok.com trigger, carrying a reply token; the run fetches the prompt and answers through the member's Outer Steppes connector ([ADR-0017](0017-prompts-and-answers-through-the-connector.md)). Until now the run learned what to do only from the automation's own prompt, a block of text each member pasted from the account page.

Facts this depends on, as of this date:
- **Runs searched the web before fetching the prompt.** In the owner's runs on 2026-10-09, Grok searched the web for the prompt, the tool's schema or the product name before calling `get_prompt`, costing time and opening a way for the run to send data out.
- **Wording decided it.** Telling the run how to search for the tools made it search. Saying the tools are already available, and that `get_prompt` is the first action, stopped the search, and web search still ran after the fetch when the prompt needed it.
- **The run reads the trigger body first,** and followed instructions placed there when the automation's prompt was only "Follow the instructions field in the trigger body exactly."
- **The body is signed** with the automation's secret, so its text comes from the bridge.
- **Prompt injection is the likeliest way a reply token leaks**, through what a run reads while answering.
- **The operator can already send a member's automation any prompt** ([ADR-0014](0014-run-your-own-bridge.md)).

## Options

### Keep the instructions in each member's automation

No change to the payload. Every member must paste the text and paste it again whenever it improves, and an old copy keeps the old behavior.

### Send the instructions in the trigger body

A fixed `instructions` field in every body, and a one-line automation prompt that says to follow it. The text is in one place, every run gets the current one, and it sits next to the token it is about. The body grows by about 1 KB.

### Give the run code to execute

The run would still have to choose to run it, the code couldn't call the connector's tools without a credential the run never sees, and code that handles the token is one more way for it to leave the run.

## Decision

Send the instructions in the trigger body, with the tested text, including the prompt-injection lines; the automation's prompt is one line (owner, 2026-10-09).

## Consequences

- The payload gains `instructions`, first, the same fixed text in every body. It holds no prompt or Discord id, so ADR-0017 stands.
- Members paste one line, once. Changing the instructions is a bridge release, not a request to every member.
- The bridge writes the run's instructions, which is no new trust: its operator could already send the automation any prompt.

## Revisit if

- grok.com runs stop following instructions in the trigger body, or grok.com starts showing the prompt or tools differently.
- Members need different instructions per automation.
