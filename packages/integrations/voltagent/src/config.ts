/**
 * Helper that builds an OTel `BatchSpanProcessor` wired to the
 * Lumin exporter — the right shape to register with VoltAgent's
 * OTel pipeline.
 *
 * Usage:
 *
 *     import { luminProcessor } from '@lumin-io/voltagent';
 *
 *     spanProcessors: [luminProcessor()]
 *
 * ENV variables read:
 *   LUMIN_HOST        — exporter host (default http://localhost:8000)
 *   LUMIN_API_KEY     — optional bearer token for hosted Lumin
 *   LUMIN_PROJECT     — project tag (default "voltagent")
 *   LUMIN_SERVICE_NAME — VoltAgent serviceName (default "voltagent-app")
 */

import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import {
  LuminExporter,
  type LuminExporterConfig,
} from './exporter.js';

export interface LuminConfigOptions extends LuminExporterConfig {
  /** VoltAgent / OTel `service.name` resource attribute. Default:
   *  LUMIN_SERVICE_NAME or "voltagent-app". */
  serviceName?: string;
}

/**
 * Build a LuminExporter wrapped in a `BatchSpanProcessor` ready
 * to be registered with VoltAgent's OTel pipeline or any OTel
 * `NodeTracerProvider`'s `spanProcessors:` array.
 */
export function luminProcessor(
  opts: LuminConfigOptions = {},
): BatchSpanProcessor {
  return new BatchSpanProcessor(
    new LuminExporter({
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
 * Build a Lumin exporter directly (useful if you want a different
 * processor, e.g. SimpleSpanProcessor for tests).
 */
export function luminExporter(
  opts: LuminConfigOptions = {},
): LuminExporter {
  return new LuminExporter({
    host: opts.host,
    apiKey: opts.apiKey,
    project: opts.project,
    timeoutMs: opts.timeoutMs,
    fetchImpl: opts.fetchImpl,
    maxPayloadSize: opts.maxPayloadSize,
  });
}

/** Read the VoltAgent service name from env (or the supplied default). */
export function resolveServiceName(opts: LuminConfigOptions = {}): string {
  const env =
    typeof process !== 'undefined' ? process.env : ({} as NodeJS.ProcessEnv);
  return opts.serviceName ?? env.LUMIN_SERVICE_NAME ?? 'voltagent-app';
}
