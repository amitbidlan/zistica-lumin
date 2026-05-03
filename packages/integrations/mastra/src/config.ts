/**
 * Helper to build the Mastra observability config block. Saves users
 * from manually typing the nested `observability.configs.synaptic.*`
 * shape — and reads sensible defaults from environment variables.
 *
 * Usage:
 *
 *     import { Mastra } from "@mastra/core";
 *     import { synapticConfig } from "@synaptic/mastra";
 *
 *     export const mastra = new Mastra({
 *       agents: { myAgent },
 *       observability: synapticConfig(),  // reads SYNAPTIC_HOST etc.
 *     });
 *
 * ENV variables read:
 *   SYNAPTIC_HOST        — exporter host (default http://localhost:8000)
 *   SYNAPTIC_API_KEY     — optional bearer token for hosted Synaptic
 *   SYNAPTIC_PROJECT     — project tag (default "mastra")
 *   SYNAPTIC_SERVICE_NAME — Mastra serviceName (default "mastra-app")
 */

import {
  SynapticExporter,
  type SynapticExporterConfig,
} from './exporter.js';

export interface SynapticConfigOptions extends SynapticExporterConfig {
  /** Mastra `serviceName`. Default: SYNAPTIC_SERVICE_NAME or "mastra-app". */
  serviceName?: string;
  /** Config block name. Default: "synaptic". Mastra allows multiple
   *  observability configs side-by-side. */
  configName?: string;
}

/**
 * Build the full Mastra `observability` block with the SynapticExporter
 * pre-wired. Returned shape matches Mastra v1.x:
 *
 *   { configs: { synaptic: { serviceName, exporters: [...] } } }
 */
export function synapticConfig(opts: SynapticConfigOptions = {}): {
  configs: Record<
    string,
    { serviceName: string; exporters: SynapticExporter[] }
  >;
} {
  const env = typeof process !== 'undefined' ? process.env : ({} as NodeJS.ProcessEnv);
  const serviceName =
    opts.serviceName ?? env.SYNAPTIC_SERVICE_NAME ?? 'mastra-app';
  const configName = opts.configName ?? 'synaptic';

  const exporter = new SynapticExporter({
    host: opts.host,
    apiKey: opts.apiKey,
    project: opts.project,
    timeoutMs: opts.timeoutMs,
    fetchImpl: opts.fetchImpl,
    maxPayloadSize: opts.maxPayloadSize,
  });

  return {
    configs: {
      [configName]: {
        serviceName,
        exporters: [exporter],
      },
    },
  };
}
