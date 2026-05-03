/**
 * Side-effect entry point — `import "@lumin-io/openclaw/auto"` enables
 * tracing when LUMIN_TRACING is truthy.
 *
 * Honest caveat: modern OpenTelemetry (v2+) requires SpanProcessors
 * to be passed at TracerProvider construction time. Once OpenClaw
 * has built its provider, you can no longer attach a processor to
 * it from the outside. So this module can only attach when:
 *
 *   1. LUMIN_TRACING is set, AND
 *   2. The user has either registered a custom TracerProvider that
 *      still exposes a public `addSpanProcessor` (older OTel SDK
 *      versions), OR exposes a `getDelegate()` that returns one.
 *
 * For OpenClaw v1+ the supported path is to wire the processor
 * directly into `@openclaw/diagnostics-otel`'s configuration, e.g.:
 *
 *     import { luminProcessor } from '@lumin-io/openclaw';
 *     // … pass luminProcessor() into the diagnostics config
 *
 * This module is a courtesy hook for environments where late
 * attachment works.
 */

import { trace, type TracerProvider } from '@opentelemetry/api';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { LuminExporter } from './exporter.js';

interface ProviderWithProcessor extends TracerProvider {
  addSpanProcessor?: (processor: BatchSpanProcessor) => void;
  getDelegate?: () => TracerProvider;
}

function isEnabled(): boolean {
  if (typeof process === 'undefined') return false;
  const v = (process.env.LUMIN_TRACING ?? '').toLowerCase();
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
 * walk to the underlying SDK and attach a LuminExporter. Returns
 * true if attached, false if no compatible provider was found.
 */
export function tryAttach(provider: TracerProvider): boolean {
  const target = findAttachableProvider(provider);
  if (target === null) return false;
  target.addSpanProcessor!(new BatchSpanProcessor(new LuminExporter()));
  return true;
}

/**
 * Attach a LuminExporter to the active OTel pipeline if
 * LUMIN_TRACING is set. Returns true on successful attach,
 * false if disabled or no compatible provider was reachable.
 */
export function installLuminTracing(): boolean {
  if (!isEnabled()) return false;
  return tryAttach(trace.getTracerProvider());
}

// Run on import — best-effort. If the provider isn't ready yet,
// fall back to the explicit `luminProcessor()` helper.
installLuminTracing();
