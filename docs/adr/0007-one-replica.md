# ADR-0007: One replica

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-208 (part of MALU-199; moved from DESIGN.md, Key decision 9) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 9) in MALU-199. Revision 2 of the design already ran one process and one replica. Revision 3, on 2026-09-27, made it a Key decision after the review of revision 2 pointed out that a second replica would be a correctness bug, not a scale-out step. |

## Context

Several guarantees rest on a single writer:
- **Dedup.** Discord delivers interactions at least once. The bridge claims `interaction.id` before any policy check, so a retried delivery replays the stored ACK.
- **Exactly one POST.** Each accepted `/ask` gets one signed POST ([ADR-0002](0002-never-sign-twice.md)).
- **Admission.** Single-flight, the outstanding cap, rate limits and the queue limit are decided in one critical section: one in-process lock around one `BEGIN IMMEDIATE` transaction.
- **In-process state.** Workers are registered in a process-wide map, and the finalizer and sweeper run as tasks in the same process.

The expected load is one person's bridge, or a few people who trust its operator ([ADR-0014](0014-run-your-own-bridge.md)).

## Options

### Multiple replicas

Survives one process failing and scales out. Every guarantee above would need a shared store and a distributed lock (Postgres, Redis or similar), and the in-process maps would have to move into it. Without them, two replicas could both claim an interaction or both POST it, starting two runs on the member's account.

### One process, one replica, SQLite

Dedup and admission use SQLite's single writer and one in-process lock. Nothing else to deploy.

## Decision

One replica. A double run on a member's account is the failure this design works hardest to avoid, and one bridge's load doesn't need more than one process.

## Consequences

- Correctness depends on a single writer. Running a second replica against the same database, or a copy of it, is unsupported, and the threat model lists it.
- No multiple replicas, Postgres or Redis are planned.
- A restart is downtime. In-flight work survives it through the database ([ADR-0003](0003-interaction-tokens-are-stored.md)).

## Revisit if

- A bridge needs to serve more traffic than one process can handle, or can't tolerate restart downtime.
