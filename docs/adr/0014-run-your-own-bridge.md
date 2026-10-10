# ADR-0014: Run your own bridge

| Field | Value |
| --- | --- |
| Status | Partly superseded by [ADR-0016](0016-personal-by-default-claimed-through-a-link.md): the consequence that the defaults (`SELF_SERVE`, `MAX_MEMBERS`) are unchanged for now |
| Opened | 2026-10-05 |
| Merged | 2026-10-06 |
| Linear | MALU-210 (part of MALU-199; moved from DESIGN.md, Key decision 17) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 17) in MALU-199. The decision was made in MALU-181 and merged in the fresh `outer-steppes` repository's #1 on 2026-10-05 (a7572a9), then ported to this repository in #39 on 2026-10-06. |

## Context

Whoever runs a bridge (the *operator*) is in a position no code can restrain:
- The bridge holds each member's automation URL and `whsec_` in plaintext ([ADR-0005](0005-webhook-secrets-in-plaintext-tokens-hashed.md)). With those, the operator can send that member's automation any prompt, at any time. The run uses the member's grok.com connectors (mail, drive, files, and so on), and its answer comes back to the operator.
- Every prompt and answer passes through the operator's process.
- Connecting the connector alone is enough for the rest: a modified bridge controls the MCP tools, descriptions and results the member's Grok sees, and can use them to steer runs that load the connector, which carry the member's other connectors.
- A member can't verify what a hosted bridge runs.

The defaults at the time (`SELF_SERVE` on, an empty `USER_ALLOWLIST`, `MAX_MEMBERS` 100) let any Discord user join a bridge that has `DISCORD_CLIENT_SECRET` set.

## Options

### Present a hosted bridge as a service anyone can join

Members would hand a stranger their grok.com account and everything connected to it.

### Limit the code to a single member

It would drop the trusted-group case, such as a household, and protect nobody, since an operator can change the code.

### Recommend one bridge per person, and support sharing only among people who trust the operator

The recommended setup is one bridge per person, on a machine they control, with themselves as its only member. Running one for other people stays supported, but only for people who would trust the operator with their grok.com account and everything connected to it (a household, close friends).

## Decision

Recommend running your own bridge, and support sharing only among people who trust the operator that far. The operator's access can't be engineered away, so the answer is who runs the bridge, not what the code does.

## Consequences

- The operator is trusted by design. The threat model's "malicious or compromised operator" row is accepted, and the only mitigation is self-hosting.
- The README leads with this, a personal bridge sets `USER_ALLOWLIST` to its owner, and SECURITY.md puts the operator's own access out of scope.
- Defaults (`SELF_SERVE`, `MAX_MEMBERS`) are unchanged for now. Changing them is a behavior change for its own PR, and it would quietly close existing shared bridges to newcomers on upgrade. Whether to change them is an open question (MALU-231).
- A warning on the account page, before a member pastes their secret, is planned as a later PR that changes the page (MALU-182 in Linear). It wasn't designed at the time, and gets designed in DESIGN.md before that PR.

## Revisit if

- The open question is settled by changing the defaults, which would make this decision's "unchanged for now" consequence out of date and may warrant a new ADR.
- Something lets a member verify what a hosted bridge runs, or limits what its operator can do with a member's webhook and connector.
