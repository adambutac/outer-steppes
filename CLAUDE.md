# Working on Outer Steppes

DESIGN.md says what the system does; `docs/adr/` says why, for product-architecture decisions only (its README has the test). Change DESIGN.md before the behavior, and keep the two in step. A PR that makes or reverses such a decision adds its ADR or supersedes the old one; any other reason goes next to its rule. Merged ADRs aren't rewritten.

**No users yet.** There's no release and no users, so a change simply changes the behavior: no upgrade paths, migrations, compatibility shims, deprecation notes or handling for existing bridges. Answer a review finding that asks for one with this rule. Deploy steps, such as order or a snapshot before a schema change, aren't upgrade paths and stay. This holds until the owner announces a release or the public cutover (MALU-160).

**A design ahead of its code** goes up as a draft PR for the owner to review the design, and merges with its build as one PR with green CI, since tests compare DESIGN.md with the code and an ADR merges with the change it decides (owner, 2026-10-08).
- While the build is missing, DESIGN.md → What exists names the unbuilt change (and its ADR, if any), and says README.md, SECURITY.md and `docs/outer-steppes.excalidraw` still describe the running code. The PR says at the top that it's a draft for design review and which tests are expected to fail; gitleaks must pass.
- The build starts only on the owner's word, in the session or on the PR.
- During the build, a deliberate break counts as caught only by a test that passed before it.
- Keep it current by merging `main` in, never rebasing, and reconcile DESIGN.md with what merged.
- Before it leaves draft: the build is on the branch, the What exists note and the draft notice are gone, README.md, SECURITY.md and the diagram match the code, DESIGN.md's Status row names the PR, and CI is green.
- Linear: In Review while the design waits, In Progress during the build, In Review once ready.

## Pull requests

- **One concern per PR:** login/auth, registration, the request path, operations, or docs.
- **Fill in `.github/pull_request_template.md` completely.** The owner reviews through it, not the diff: name the top-risk code precisely, and be candid under decisions and unverified assumptions. Test anything a test can show; the checklist is only for live checks (DESIGN.md → Testing, "PR rule").
- **Deliberate-break check** for top-risk code: break each risky behavior on purpose, one at a time, confirm a test fails, revert, and list each in the PR's table. A break no test catches means a missing test: write it and re-run.
- **Independent review before every push.** Commit, then run the `pr-reviewer` agent with exactly "Review the current branch against main." and nothing more. Fix each finding or answer it in the PR's Independent review section. Push back when you disagree ("Answered, disagree:" and the reason); don't change something only because a finding asked. A finding that questions a deliberate decision means it isn't written down: fix DESIGN.md, the ADR or a code comment. Disagreeing on top-risk code also goes under "Decisions made on your behalf".
- **A security weakness already on `main`** follows DESIGN.md → Security → Vulnerability handling: while the repo is private it goes in the PR's Independent review and the threat model; once public, never describe or hint at it in a PR, commit, issue, code comment, test name, DESIGN.md or ADR; tell the owner in the session. A fix for an advisory pushes only to the advisory's private fork, with neutral branch names and messages; the review and break check still apply.
- **CI** (tests on Linux and Windows, gitleaks) is green before merge, except for an advisory fix.
- **Merging, pushing to `main` and force-pushing are the owner's.** The pre-commit hook and `.claude/settings.json` guard against them only best-effort (GitHub doesn't enforce branch rules on this free private repo), so the rule stands on its own. The ADR merge-date stamp is a workflow's commit, not a session's.
- Committing, pushing the work's own branch and opening its PR need no fresh approval once the review, and the break check where it applies, are done; the owner still reviews and merges every PR (owner, 2026-10-07). Any other outward action needs asking.

## Linear

Plans and progress live in the Linear project **Outer Steppes** (team Malu), one issue per PR, so any session can pick up; Linear rather than GitHub issues gives the owner one planning view across projects. DESIGN.md holds what, ADRs why, the PR the review, and Linear order, scope and progress: link between them rather than copy. Todo is next, Backlog later; priority ranks within each.
- Start by reading the issue and its latest comments. Look for a planned issue before creating one.
- Comment at each checkpoint (a commit, a review round, a break check, stopping): the current and next step, open findings, dead ends, anything unverified.
- Status: In Progress while building, In Review once the PR is open (a draft design PR differs, above), Done once the owner merges (the next session checks `gh pr view`), Canceled if closed unmerged. Attach the PR to the issue, and put the issue on the PR's `Linear:` line.
- DESIGN.md's Status row names the last merged implementation PR: an implementation PR sets it to its own number on its branch (the next after the repo's latest issue or PR; check it once the PR is open). Docs-only PRs don't touch it.
- Linear writes in this project need no approval; deleting anything, or other projects, do.
- Linear is external: no tokens, `whsec_` values, automation URLs, prompt text, `.env` contents or raw logs. An open security finding is only "open finding, see advisory GHSA-…", or while the repo is private, "see PR #N, Independent review".

## Safety

- **Never `git add -A` or `git add .`**: the repo root holds a real `.env`. Stage files by name.
- The server never holds the bot token. Only `scripts/osctl_command.py` uses it, from a separate shell, never in the container (ADR-0008).
- Never log, print or commit tokens, secrets, `whsec_` values, automation URLs, claim links or prompt text. Tests use made-up ids and URLs.

## Commands

- Tests: `.venv/Scripts/python -m pytest -q`. `.venv/Scripts/osctl <group> --help` lists each group's commands.
- `osctl container up|restart|reset|restore` print a live claim link on an unclaimed bridge, as `osctl bridge claim` does. Never paste it anywhere, and ask before `restart`, `reset` or `restore` on an unclaimed bridge: the new link replaces the one the owner may be using.
- `osctl command get` output can hold channel messages: never paste it into Linear, a PR, a commit or an issue.
