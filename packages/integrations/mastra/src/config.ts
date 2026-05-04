/**
 * Helper to build the Mastra observability config block. Saves users
 * from manually typing the nested `observability.configs.lumin.*`
 * shape — and reads sensible defaults from environment variables.
 *
 * Usage:
 *
 *     import { Mastra } from "@mastra/core";
 *     import { luminConfig } from "@lumin-io/mastra";
 *
 *     export const mastra = new Mastra({
 *       agents: { myAgent },
 *       observability: luminConfig(),  // reads LUMIN_HOST etc.
 *     });
 *
 * ENV variables read:
 *   LUMIN_HOST        — exporter host (default http://localhost:8000)
 *   LUMIN_API_KEY     — optional bearer token for hosted Lumin
 *   LUMIN_PROJECT     — project tag (default "mastra")
 *   LUMIN_SERVICE_NAME — Mastra serviceName (default "mastra-app")
 */

import {
  LuminExporter,
  type LuminExporterConfig,
} from './exporter.js';

export interface LuminConfigOptions extends LuminExporterConfig {
  /** Mastra `serviceName`. Default: LUMIN_SERVICE_NAME or "mastra-app". */
  serviceName?: string;
  /** Config block name. Default: "lumin". Mastra allows multiple
   *  observability configs side-by-side. */
  configName?: string;
}

/**
 * Build the full Mastra `observability` block with the LuminExporter
 * pre-wired. Returned shape matches Mastra v1.x:
 *
 *   { configs: { lumin: { serviceName, exporters: [...] } } }
 */
export function luminConfig(opts: LuminConfigOptions = {}): {
  configs: Record<
    string,
    { serviceName: string; exporters: LuminExporter[] }
  >;
} {
  const env = typeof process !== 'undefined' ? process.env : ({} as NodeJS.ProcessEnv);
  const serviceName =
    opts.serviceName ?? env.LUMIN_SERVICE_NAME ?? 'mastra-app';
  const configName = opts.configName ?? 'lumin';

  const exporter = new LuminExporter({
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
