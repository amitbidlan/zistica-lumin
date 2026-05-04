/**
 * @lumin-io/voltagent — local-first observability for VoltAgent.
 *
 * VoltAgent is OTel-native: the framework sets up an OTel TracerProvider
 * and emits spans for every agent generation, tool call, and guardrail.
 * This package provides the Lumin side of that pipe — an OTel
 * `SpanExporter` plus a `BatchSpanProcessor` helper that's drop-in
 * for any VoltAgent / OTel configuration.
 *
 * Public API:
 *   - LuminExporter: OTel-compatible span exporter that ships to
 *     a running Lumin instance (default http://localhost:8000).
 *   - luminProcessor(): helper that wraps the exporter in a
 *     BatchSpanProcessor — the right shape for VoltAgent's OTel
 *     spanProcessors array.
 *   - luminExporter(): builds the bare exporter (e.g. for tests
 *     that want a SimpleSpanProcessor instead).
 *   - installLuminTracing(): attaches a LuminExporter to the
 *     active OTel tracer provider when LUMIN_TRACING=true. Most
 *     users will use the `import "@lumin-io/voltagent/auto"` form.
 */

export {
  LuminExporter,
  otelSpanToLumin,
  registerModelPrice,
} from './exporter.js';
export type { LuminExporterConfig } from './exporter.js';

export {
  luminProcessor,
  luminExporter,
  resolveServiceName,
} from './config.js';
export type { LuminConfigOptions } from './config.js';

export { installLuminTracing, tryAttach } from './auto.js';
