export { configure, getSDK, setSDK, SynapticSDK } from './sdk.js';
export { trace, span, withSpan } from './trace.js';
export { getCurrentSpan } from './context.js';
export { Span } from './span.js';
export { BoundedQueue } from './queue.js';
export { HTTPExporter } from './exporter.js';

export type { SpanData } from './span.js';
export type { SynapticConfig, ResolvedConfig } from './config.js';
export type { Exporter } from './exporter.js';
export type { TraceOptions, SpanHandle } from './trace.js';
