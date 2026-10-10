# ADR-0002: Never sign twice; 429 and 503 count as not sent

| Field | Value |
| --- | --- |
| Status | Accepted |
| Opened | 2026-09-27 |
| Merged | 2026-10-06 |
| Linear | MALU-207 (part of MALU-199; moved from DESIGN.md, Key decision 2) |
| Supersedes | None |
| Origin | Moved from DESIGN.md's Key decisions table (row 2) and Alternatives considered (item 3) in MALU-199. "Never sign twice" was in the first draft of the design on 2026-09-27. Treating 429 and 503 as not sent came with revision 3, the same day. |

## Context

Each accepted `/ask` becomes one Standard Webhooks–signed POST to the member's grok.com automation trigger, and each POST that grok.com queues starts a run on the member's account, with their connectors. Facts this depends on (xAI's [webhook docs](https://docs.x.ai/grok/automations/webhooks), retrieved 2026-09-27):
- 202 means queued. 429 and 503 mean retry later, and the docs suggest retrying them with backoff.
- The docs don't say triggers dedupe on `webhook-id`. A second signed POST for the same `/ask` may start a second run.
- A timeout, a disconnect after the request was written, or a 5xx other than 503 says nothing about whether the run was queued.

## Options

### Retry 429 and 503 with backoff, as xAI's docs suggest

The member gets an answer when grok.com is briefly busy, without re-running `/ask`. But a retry is a second signature, and if the first request was in fact queued, the member's account runs twice, with whatever their connectors do.

### Never retry, and treat 429 and 503 as ambiguous (the first draft)

Neither status proves the run wasn't queued, so the bridge leaves the row waiting for a late reply, as it does after a timeout. The member waits out the reply window for an answer that, by the docs, isn't coming.

### Never retry, and treat 429 and 503 as not sent

The bridge signs once. 429 and 503 fail the row at once, release single-flight, and tell the member "Grok is busy. Try again in a minute." A timeout or another 5xx is ambiguous: the row stays `posted`, so a late reply can still land, and nothing is retried.

## Decision

Never sign twice, and count 429 and 503 as not sent. A double run on someone's account, with their connectors, is worse than asking them to re-run `/ask`. Reading 429 and 503 as "not queued", as xAI's docs describe them, tells the member straight away instead of leaving them waiting for 14 minutes.

## Consequences

- Exactly one signed POST per accepted `/ask`. The signing secret is copied for that POST and discarded, never re-signed from memory.
- A member may need to re-run `/ask` when grok.com is busy.
- One assumption is carried on purpose: per xAI's docs, 429 and 503 mean the trigger was **not queued**. If that's wrong, a member's retry after a 429 or 503 could double-run. The pilot watches for it.
- An ambiguous failure leaves no interim message on the deferred response, because it could overwrite an answer arriving at the same moment. The sweeper's expiry message says Grok didn't confirm the request.

## Revisit if

- xAI documents that triggers dedupe on `webhook-id`. Retrying would then be safe.
- A double run is seen after a 429 or 503, which would mean those statuses don't always mean "not queued".
