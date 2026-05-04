/**
 * @lumin-io/mastra — local-first observability for Mastra agents.
 *
 * Public API:
 *   - LuminExporter: OTel-compatible span exporter that ships to
 *     a running Lumin instance (default http://localhost:8000).
 *   - luminConfig(): helper that returns the full Mastra
 *     `observability` config block, pre-wired with a LuminExporter.
 *   - installLuminTracing(): attaches a LuminExporter to the
 *     active OTel tracer provider when LUMIN_TRACING=true. Most
 *     users will use the `import "@lumin-io/mastra/auto"` form.
 */

export {
  LuminExporter,
  otelSpanToLumin,
  registerModelPrice,
} from './exporter.js';
export type { LuminExporterConfig } from './exporter.js';

export { luminConfig } from './config.js';
export type { LuminConfigOptions } from './config.js';

export { installLuminTracing, tryAttach } from './auto.js';
