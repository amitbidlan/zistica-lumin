# [DRAFT] Honor inbound `traceparent` on the gateway HTTP/WS entrypoint

> _Target repo: `openclaw/openclaw` — gateway server.
> Drafted by the Lumin team while building the_ `@lumin-io/openclaw-diagnostics` _plugin._

## Summary

The gateway calls `createDiagnosticTraceContext()` with no arguments at every
inbound HTTP request and at every WebSocket upgrade. Because the helper
already supports `parseDiagnosticTraceparent` for an `input.traceparent`
field, the fix is one line per call site: pass the inbound header through.

Without this change, any external system that triggers an OpenClaw run
(webhook plugin, gateway-mode HTTP API, message bus that forwards W3C
trace context) sees its `trace_id` silently dropped — OpenClaw mints a
fresh one. Trace pipelines (Honeycomb, Datadog, Tempo, Jaeger, Lumin)
then show two disconnected traces instead of one parent → child run.

The four-step path the Lumin team is taking to get full content fidelity
out of OpenClaw makes this visible: when the same trace_id flows from
the caller into the gateway and then from the gateway into the model
provider HTTP request (which OpenClaw _already_ does via
`withDiagnosticTraceparentHeader`), end-to-end propagation is complete
and external observability tools render a single timeline.

## Gap, with line numbers

`packages/openclaw/dist/server.impl-Fh0pCrMC.js` _(or whichever
TS source compiles into it — likely `src/gateway/server.ts`)_:

```js
// line ~658
function handleRequestWithTrace(req, res) {
    return runWithDiagnosticTraceContext(
        createDiagnosticTraceContext(),         // ← no inbound header
        () => handleRequest(req, res),
    );
}

// line ~871 — WebSocket upgrade
httpServer.on("upgrade", (req, socket, head) => {
    runWithDiagnosticTraceContext(
        createDiagnosticTraceContext(),         // ← no inbound header
        async () => { /* … */ },
    );
});
```

Both call sites unconditionally generate a fresh trace context.

## Fix

```diff
+ // Read W3C `traceparent` from the inbound request, if any. Multiple
+ // values would be a protocol bug (per W3C the header is single-valued)
+ // — take the first only, ignore the rest.
+ function readTraceparentHeader(req) {
+     const value = req.headers?.traceparent;
+     if (!value) return undefined;
+     return Array.isArray(value) ? value[0] : value;
+ }

  function handleRequestWithTrace(req, res) {
+     const traceparent = readTraceparentHeader(req);
      return runWithDiagnosticTraceContext(
-         createDiagnosticTraceContext(),
+         createDiagnosticTraceContext({ traceparent }),
          () => handleRequest(req, res),
      );
  }

  httpServer.on("upgrade", (req, socket, head) => {
+     const traceparent = readTraceparentHeader(req);
      runWithDiagnosticTraceContext(
-         createDiagnosticTraceContext(),
+         createDiagnosticTraceContext({ traceparent }),
          async () => { /* … */ },
      );
  });
```

`createDiagnosticTraceContext` already does the right thing here — it
parses the traceparent (validating version + length), preserves the
inbound `traceId`, and seeds a new `spanId` for the gateway's own root
span. Invalid headers fall through and behave like the current
no-argument path, so a malformed inbound header can never destabilize
the gateway.

## Why this is safe

| Concern | Resolution |
|---|---|
| Invalid `traceparent` | `parseDiagnosticTraceparent` returns `undefined`; `createDiagnosticTraceContext` falls back to `randomTraceId()` — same behavior as today. |
| Header injection / forging | Trace IDs are not authentication. The same external systems can already write to the gateway via the existing auth modes; honoring their `traceparent` doesn't widen the attack surface. |
| Sampling decision drift | The `traceFlags` byte (sampled bit) is propagated, not generated — so if the upstream chose `01`, the gateway honors it; if `00`, the gateway honors that too. This matches every other OTel gateway in the wild. |
| Multi-value header | `req.headers.traceparent` _can_ be an array under raw HTTP. The diff takes `[0]` only, matching W3C's "first wins" prescription. |
| Wider blast radius | Outbound model-call propagation already flows through `withDiagnosticTraceparentHeader` (selection-BfCSa_QL.js:4879). With this change the inbound side stitches in, the outbound side is unchanged, and end-to-end trace continuity Just Works. |

## Tests

Add to `tests/gateway/handle-request-with-trace.spec.ts`:

```ts
import { describe, it, expect, vi } from "vitest";
import {
    parseDiagnosticTraceparent,
    runWithDiagnosticTraceContext,
    getActiveDiagnosticTraceContext,
} from "openclaw/diagnostic-events";
import { handleRequestWithTrace } from "../../src/gateway/server";

describe("inbound traceparent propagation", () => {
    const TP = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1111111111111111-01";

    it("honors a valid traceparent on HTTP", async () => {
        const seen: string[] = [];
        const fakeReq = {
            url: "/v1/something",
            method: "POST",
            headers: { traceparent: TP },
        } as any;
        const fakeRes = { writeHead: vi.fn(), end: vi.fn() } as any;

        await handleRequestWithTrace(fakeReq, fakeRes, () => {
            seen.push(getActiveDiagnosticTraceContext()!.traceId);
        });

        expect(seen[0]).toBe("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    });

    it("falls back to a fresh trace when traceparent is missing", async () => {
        const seen: string[] = [];
        await handleRequestWithTrace(
            { url: "/", method: "GET", headers: {} } as any,
            { writeHead: vi.fn(), end: vi.fn() } as any,
            () => seen.push(getActiveDiagnosticTraceContext()!.traceId),
        );
        expect(seen[0]).toMatch(/^[0-9a-f]{32}$/);
        expect(seen[0]).not.toBe("00000000000000000000000000000000");
    });

    it("falls back when traceparent is malformed", async () => {
        const seen: string[] = [];
        await handleRequestWithTrace(
            { url: "/", method: "GET", headers: { traceparent: "lol-not-real" } } as any,
            { writeHead: vi.fn(), end: vi.fn() } as any,
            () => seen.push(getActiveDiagnosticTraceContext()!.traceId),
        );
        expect(seen[0]).toMatch(/^[0-9a-f]{32}$/);
    });

    it("honors the array form (RFC 7230 multi-value)", async () => {
        const seen: string[] = [];
        await handleRequestWithTrace(
            { url: "/", method: "GET", headers: { traceparent: [TP, "garbage"] } } as any,
            { writeHead: vi.fn(), end: vi.fn() } as any,
            () => seen.push(getActiveDiagnosticTraceContext()!.traceId),
        );
        expect(seen[0]).toBe("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    });
});
```

A symmetric set of tests should cover the WebSocket upgrade path,
asserting the same behavior using a fake `IncomingMessage` carrying
the header in `req.headers`.

## Optional follow-up (not part of this PR)

Consider also reading `tracestate` and threading it through the
context. OpenClaw's internal model already treats traceparent as the
sole vendor-neutral header; `tracestate` is where vendor-specific
context lives (e.g. Honeycomb's sample-rate hints). It's strictly
additive — no callers today, no risk of regressions — but expanding
the public diagnostic-trace-context shape touches more files, so it
makes sense to keep it out of this minimal fix and ship separately.

## Operator-visible behavior change

Before:
```
[caller traceparent: 00-T1-S1-01]
  └── gateway run [trace=T2 (random), parent=none]    ← disconnected
        └── model call [trace=T2, parent=…]
```

After:
```
[caller traceparent: 00-T1-S1-01]
  └── gateway run [trace=T1, parent=S1]               ← stitched
        └── model call [trace=T1, parent=…]
```

A two-line config change in OpenTelemetry collectors / sampling
processors becomes _unnecessary_; the trace is contiguous by virtue
of the gateway respecting what its caller already declared.
