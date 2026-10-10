# Security policy

## Reporting a vulnerability

**Report privately, through GitHub:** open the repository's **Security** tab and choose **Report a vulnerability**. Please don't open a public issue or pull request, or post details anywhere else, until the advisory for it is published.

A useful report says:
- what an attacker can do, and what they need first (a Discord account, a member's browser, the bot token, network position, and so on)
- the steps or a proof of concept
- the commit you tested

Test only against a bridge you run yourself, with your own Discord application and grok.com account. Don't test against someone else's deployment or other people's accounts.

## Scope

**In scope:** the code in this repository. That is the bridge (Discord interactions, the MCP server and its OAuth, the Discord login, the account page, signing, the automation URL policy, the admin listener and `osctl`), plus `deploy/Dockerfile`.

**Out of scope, report to the vendor instead:**
- grok.com, its automations, and its MCP connectors: xAI
- Discord itself: Discord's own security program

**Known and accepted:** [DESIGN.md → Security → Threat model](DESIGN.md#threat-model) lists residual and accepted risks (for example, what a stolen session cookie allows). Reports of those are welcome only if they show the risk is worse than the threat model says.

**The operator is trusted by design.** The operator, who runs a bridge and its host, can use its members' grok.com automations and connectors and read their prompts and answers. The answer to that is running your own bridge, not a code fix ([ADR-0014](docs/adr/0014-run-your-own-bridge.md)). So "the operator can do this" isn't a vulnerability. A way for anyone *else* to get that access is. Nor is it a vulnerability that a bridge's defaults let any Discord user join it: that's a known open question ([DESIGN.md → Deployment and rollout](DESIGN.md#deployment-and-rollout), step 8).

Also out of scope: volume-based denial of service, and weaknesses of a particular deployment's own host, proxy, or settings that don't come from this repository's code or defaults.

## Supported versions

There are no releases. Only the latest commit on `main` gets security fixes, and operators update by deploying it.

## What to expect

This is a small project maintained in spare time, so these are aims, not guarantees:
- an acknowledgement within 7 days
- an assessment (accepted or not, and why) within 14 days
- a fix, or a plan with a date, within 90 days of the report

The fix is built privately. When it is merged, a GitHub security advisory is published with the details, and it credits you unless you ask not to be named. Please keep the details private until then. If 90 days pass without a fix or a plan, say so in the report's thread before you disclose.

How findings are handled internally is in [DESIGN.md → Security → Vulnerability handling](DESIGN.md#vulnerability-handling).
