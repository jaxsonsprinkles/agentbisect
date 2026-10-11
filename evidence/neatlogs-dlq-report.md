# neatlogs: multi-span traces finalize as `dlq` with 0 spans

Project `110108af-1737-47ee-8494-cf482d13defd` (neatlogs-us, https://app.neatlogs.com). Python SDK 1.4.26, Python 3.13, 2026-10-10.

## Summary

Traces with more than one span end in `finalizationStatus: "dlq"` with `spansCount: 0`. The SDK reports 0 export failures and 0 drops (`get_delivery_diagnostics()`), so the spans leave the client. The backend then leaves the trace `pending` for about 7 minutes and dead-letters it.

It is not shape-dependent: the same code produced a finalized trace once and `dlq` twice afterwards. Single-span traces from earlier the same day (21:23 to 21:24 UTC) finalized fine.

## Minimal repro

A bare script: one `WORKFLOW` span with one `TOOL` child via `neatlogs.trace()`, then `flush()` and `shutdown()`. No model calls and no application code. Run it 3 times.

| Run | Trace id | Result |
|---|---|---|
| 1 | `af41536424d272b3696bdc616e40143e` | finalized, 2 spans |
| 2 | `99a3a8d5ad662f4bac4cbc47e964ffc5` | `dlq`, 0 spans |
| 3 | `a25f8e0c33f84eaa716ac6eab355050e` | `dlq`, 0 spans |

Other variants (all `dlq`, 0 spans, or never ingested):

- `WORKFLOW` + 2 `TOOL` siblings: `c29bcec815dab4068db3766f06beab6d`, `fadd81a6bba43dc8a84215164a66c0d2`
- Nested or 3+ spans: `ed094ad96d4c040d4ef645f5f01d633a`, `75ad436dc68f7541fa76743668031d99`, `2e1c41856ca841bc5d6d2d9605b29c26`
- Real app runs (`support_case` parent with `tool:*` children and wrapped model calls): `5ad517664ed4dbd323aaf32526ceacbf` (dlq), `dca67a22affcc988c9af943794aae4fe` (dlq), `f502464ebbce39102545d28ddae32435` (NOT_FOUND on read)
- Never ingested (NOT_FOUND): `2efe8dec27efcadc3bce6466972b912b`, `66be775a76c204969e15d8f947d2a10e`, `a155af5daf3848ccf7bc667ea06874c1`, `91b8c7dfc660f1032c34685f19cae705`, `353ec64dcc8643d324c44031ada21bc5`

## Ruled out on our side

- Attribute types (string, int, JSON-encoded): no difference.
- Tags (empty vs non-empty): no difference.
- Wrapped model calls (`neatlogs.wrap`): failures reproduce without them.
- SDK export: 0 failures, 0 drops.

## What we need

1. Why these traces were dead-lettered (backend log or DLQ reason for the ids above).
2. Whether there is a known SDK or ingest constraint for nested or multi-span traces that we are missing.
3. A way to re-drive the dlq traces, if possible.

## Why it matters

AgentBisect (neatHack) uses neatlogs traces to find where a failing agent run first diverges from a good one. It needs span-level data (tool calls with args and results) and cannot get it while multi-span traces dead-letter.
