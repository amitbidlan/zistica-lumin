/**
 * @synaptic/mastra — local-first observability for Mastra agents.
 *
 * Public API:
 *   - SynapticExporter: OTel-compatible span exporter that ships to
 *     a running Synaptic instance (default http://localhost:8000).
 *   - synapticConfig(): helper that returns the full Mastra
 *     `observability` config block, pre-wired with a SynapticExporter.
 *   - installSynapticTracing(): attaches a SynapticExporter to the
 *     active OTel tracer provider when SYNAPTIC_TRACING=true. Most
 *     users will use the `import "@synaptic/mastra/auto"` form.
 */

export { SynapticExporter, otelSpanToSynaptic } from './exporter.js';
export type { SynapticExporterConfig } from './exporter.js';

export { synapticConfig } from './config.js';
export type { SynapticConfigOptions } from './config.js';

export { installSynapticTracing, tryAttach } from './auto.js';
