/**
 * Helper that builds an OTel `BatchSpanProcessor` wired to the
 * Synaptic exporter — the right shape to register with
 * `@openclaw/diagnostics-otel` (or any other OTel pipeline).
 *
 * Usage:
 *
 *     import { synapticProcessor } from '@synaptic/openclaw';
 *
 *     // Pass to the OTel SDK / OpenClaw diagnostics config:
 *     spanProcessors: [synapticProcessor()]
 *
 * ENV variables read:
 *   SYNAPTIC_HOST        — exporter host (default http://localhost:8000)
 *   SYNAPTIC_API_KEY     — optional bearer token for hosted Synaptic
 *   SYNAPTIC_PROJECT     — project tag (default "openclaw")
 *   SYNAPTIC_SERVICE_NAME — OpenClaw serviceName (default "openclaw-app")
 */

import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import {
  SynapticExporter,
  type SynapticExporterConfig,
} from './exporter.js';

export interface SynapticConfigOptions extends SynapticExporterConfig {
  /** OpenClaw / OTel `service.name` resource attribute. Default:
   *  SYNAPTIC_SERVICE_NAME or "openclaw-app". */
  serviceName?: string;
}

/**
 * Build a SynapticExporter wrapped in a `BatchSpanProcessor` ready
 * to be registered with `@openclaw/diagnostics-otel` or any OTel
 * `NodeTracerProvider`'s `spanProcessors:` array.
 */
export function synapticProcessor(
  opts: SynapticConfigOptions = {},
): BatchSpanProcessor {
  return new BatchSpanProcessor(
    new SynapticExporter({
      host: opts.host,
      apiKey: opts.apiKey,
      project: opts.project,
      timeoutMs: opts.timeoutMs,
      fetchImpl: opts.fetchImpl,
      maxPayloadSize: opts.maxPayloadSize,
    }),
  );
}

/**
 * Build a Synaptic exporter directly (useful if you want a different
 * processor, e.g. SimpleSpanProcessor for tests).
 */
export function synapticExporter(
  opts: SynapticConfigOptions = {},
): SynapticExporter {
  return new SynapticExporter({
    host: opts.host,
    apiKey: opts.apiKey,
    project: opts.project,
    timeoutMs: opts.timeoutMs,
    fetchImpl: opts.fetchImpl,
    maxPayloadSize: opts.maxPayloadSize,
  });
}

/** Read the OpenClaw service name from env (or the supplied default). */
export function resolveServiceName(opts: SynapticConfigOptions = {}): string {
  const env =
    typeof process !== 'undefined' ? process.env : ({} as NodeJS.ProcessEnv);
  return opts.serviceName ?? env.SYNAPTIC_SERVICE_NAME ?? 'openclaw-app';
}
