# ADR-0013: License: AGPL-3.0-only

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-10-05 |
| Merged | 2026-10-06 |
| Linear | MALU-210 (part of MALU-199; moved from DESIGN.md, Key decision 16) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 16) in MALU-199. The decision was made in MALU-163 and merged in #36 on 2026-10-05 (008753b). |

## Context

The project had no license. The owner wants anyone who runs a modified bridge as a service for others to share their changes. The bridge is a network service: people use it through Discord and grok.com, and nobody receives a copy of it. It is a hobby project, so a license's cost to commercial adoption doesn't matter.

The runtime dependencies, aiohttp and PyNaCl, are Apache-2.0.

## Options

### MIT or Apache-2.0

Permissive. A hosted fork could stay closed.

### GPL-3.0

Copyleft, but only distribution triggers it. Running a modified bridge as a service isn't distribution, so a hosted fork could still stay closed.

### MPL-2.0

File-level copyleft, and like the GPL it isn't triggered by hosting.

### AGPL-3.0-or-later

The AGPL, with later versions applying automatically, so a future license version the owner hasn't read would apply without the owner's choice.

### AGPL-3.0-only

The AGPL's section 13 is the only one of these that reaches hosted use: someone who modifies the bridge and lets others use it over a network must offer those users its source.

## Decision

AGPL-3.0-only (`LICENSE`, and `license = "AGPL-3.0-only"` in `pyproject.toml`). It is the only option that makes a modified, hosted bridge share its changes, and "only" keeps future GPL versions from applying without the owner's choice.

## Consequences

- Operators of a modified bridge must offer its source to the people using it.
- The bridge has no built-in "source" link. Section 13 binds only someone who modifies the bridge, and the unmodified bridge's source is the project's public repository, so a fork's operator must add the offer themselves. A built-in offer, which forks would inherit, is a possible later item, not part of this decision.
- The container image carries `LICENSE` (`deploy/Dockerfile`), and the README names the copyright holder (Adam Butac).
- Contributions come in under the same license (inbound = outbound), with no CLA or DCO. Relicensing later needs every outside contributor's agreement.
- The runtime dependencies are Apache-2.0, which the GPLv3 family accepts. A new dependency needs a license compatible with AGPL-3.0.

## Revisit if

The owner wants to allow closed hosted forks, or the project stops being a hobby and commercial adoption starts to matter. Either way, relicensing needs every outside contributor's agreement by then.
