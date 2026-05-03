/**
 * Side-effect entry point — `import "@synaptic/mastra/auto"` enables
 * tracing when SYNAPTIC_TRACING is truthy.
 *
 * Honest caveat: modern OpenTelemetry (v2+) requires SpanProcessors
 * to be passed at TracerProvider construction time. Once Mastra has
 * built its provider, you can no longer attach a processor to it
 * from the outside. So this module can only attach when:
 *
 *   1. SYNAPTIC_TRACING is set, AND
 *   2. The user has either registered a custom TracerProvider that
 *      still exposes a public `addSpanProcessor` (older OTel SDK
 *      versions), OR exposes a `getDelegate()` that returns one.
 *
 * For Mastra v1+, the supported path is the explicit
 * `synapticConfig()` helper — see the package README. This module
 * is a courtesy hook for environments where late attachment works.
 */

import { trace, type TracerProvider } from '@opentelemetry/api';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { SynapticExporter } from './exporter.js';

interface ProviderWithProcessor extends TracerProvider {
  addSpanProcessor?: (processor: BatchSpanProcessor) => void;
  getDelegate?: () => TracerProvider;
}

function isEnabled(): boolean {
  if (typeof process === 'undefined') return false;
  const v = (process.env.SYNAPTIC_TRACING ?? '').toLowerCase();
  return v === 'true' || v === '1';
}

/**
 * Walks the proxy chain looking for a provider that exposes
 * addSpanProcessor. ProxyTracerProvider (the default global) wraps
 * the user-supplied provider once; we follow the delegate to reach
 * the real one.
 */
function findAttachableProvider(
  start: TracerProvider,
): ProviderWithProcessor | null {
  let p: ProviderWithProcessor | null = start as ProviderWithProcessor;
  for (let i = 0; i < 5 && p != null; i++) {
    if (typeof p.addSpanProcessor === 'function') return p;
    if (typeof p.getDelegate === 'function') {
      p = p.getDelegate() as ProviderWithProcessor;
    } else {
      break;
    }
  }
  return null;
}

/**
 * Pure attachment logic — exposed for tests. Given a TracerProvider,
 * walk to the underlying SDK and attach a SynapticExporter. Returns
 * true if attached, false if no compatible provider was found.
 */
export function tryAttach(provider: TracerProvider): boolean {
  const target = findAttachableProvider(provider);
  if (target === null) return false;
  target.addSpanProcessor!(new BatchSpanProcessor(new SynapticExporter()));
  return true;
}

/**
 * Attach a SynapticExporter to the active OTel pipeline if
 * SYNAPTIC_TRACING is set. Returns true on successful attach,
 * false if disabled or no compatible provider was reachable.
 *
 * NOT idempotent across calls — attaches once per call when
 * conditions are met. The auto-load on import means most users
 * will call this exactly once at startup.
 */
export function installSynapticTracing(): boolean {
  if (!isEnabled()) return false;
  return tryAttach(trace.getTracerProvider());
}

// Run on import — best-effort. If the provider isn't ready yet, the
// caller can re-invoke `installSynapticTracing()` after their
// framework boots, or fall back to `synapticConfig()`.
installSynapticTracing();
