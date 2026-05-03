/**
 * @synaptic/openclaw — local-first observability for OpenClaw agents.
 *
 * OpenClaw emits OTel telemetry through `@openclaw/diagnostics-otel`.
 * This package provides the Synaptic side of that pipe — an OTel
 * `SpanExporter` plus a `BatchSpanProcessor` helper that's drop-in
 * for any OpenClaw / OTel configuration.
 *
 * Public API:
 *   - SynapticExporter: OTel-compatible span exporter that ships to
 *     a running Synaptic instance (default http://localhost:8000).
 *   - synapticProcessor(): helper that wraps the exporter in a
 *     BatchSpanProcessor — the right shape for OpenClaw's
 *     diagnostics-otel config.
 *   - synapticExporter(): builds the bare exporter (e.g. for tests
 *     that want a SimpleSpanProcessor instead).
 *   - installSynapticTracing(): attaches a SynapticExporter to the
 *     active OTel tracer provider when SYNAPTIC_TRACING=true. Most
 *     users will use the `import "@synaptic/openclaw/auto"` form.
 */

export {
  SynapticExporter,
  otelSpanToSynaptic,
  registerModelPrice,
} from './exporter.js';
export type { SynapticExporterConfig } from './exporter.js';

export {
  synapticProcessor,
  synapticExporter,
  resolveServiceName,
} from './config.js';
export type { SynapticConfigOptions } from './config.js';

export { installSynapticTracing, tryAttach } from './auto.js';
