Linear: <!-- issue id, e.g. MALU-19 -->

<!-- A draft design PR (CLAUDE.md, "A design ahead of its code") replaces this comment with a visible note: it's a draft for design review, and which tests are expected to fail and why. Otherwise delete it. -->

## What changes for users

<!-- Behavior, not code: what a member or the operator will notice. -->

## Read these (top risk tier)

<!-- The few files/functions worth a human read: trust boundaries, secrets,
     outbound requests, anything irreversible. Everything else is covered by
     tests. Name them precisely, e.g. `oauth.py: OAuthServer._exchange_code`. -->

-

## Decisions made on your behalf

<!-- Anything the author chose that wasn't spelled out in the request, DESIGN.md, or an ADR.
     A decision that meets docs/adr/README.md → When to write one gets its own ADR in this PR;
     any other decision gets its reason next to its rule. -->

-

## Assumptions not verified, and least-confident areas

-

## Acceptance checklist (live checks only)

<!-- Only what a test can't sufficiently cover (DESIGN.md → Testing): how Discord
     renders something, the real grok.com, TLS, deploy order, the owner's own data.
     Each item: what to try, the expected result, and why no test covers it.
     Anything an integration or unit test can show belongs in a test, not here.
     If there is nothing, write "None" and why. -->

- [ ]

## Deliberate-break check

<!-- For each top-risk behavior: break it on purpose, run the suite, revert.
     Shows that the tests catch what they claim to. "None" in the second column
     means a test was missing: add it and re-run. Omit for PRs with no top-risk code. -->

| Deliberate break | Caught by |
| --- | --- |
| | |

## Independent review

<!-- From the pr-reviewer agent (no session context), run before the push.
     Verdict, then each finding and what was done: fixed (how), clarified in
     DESIGN.md/code (where), answered (why), or "Answered, disagree:" (why not,
     per CLAUDE.md). The repo is public, so a security weakness in code
     already on main is left out of this table
     entirely, and the verdict is recorded as it stands without it
     (DESIGN.md → Security → Vulnerability handling).
     The verdict stays visible; the findings table goes in the collapsed block,
     whose summary gives the rounds and finding count, e.g. "3 rounds, 6 findings:
     4 fixed, 1 answered, 1 disagreed". The count covers only the table's rows, so an
     omitted finding doesn't show in it either. -->

Verdict:

<details>
<summary>Findings: </summary>

| Finding | Severity | Resolution |
| --- | --- | --- |
| | | |

</details>

## Checks

- [ ] CI green (tests on Linux and Windows, gitleaks)
- [ ] DESIGN.md / README updated if behavior or config changed
- [ ] ADR added (or an old one superseded) if this PR makes or reverses a decision that needs one (`docs/adr/README.md` → When to write one)
- [ ] Files staged by name; no `.env`, database, or secret material
