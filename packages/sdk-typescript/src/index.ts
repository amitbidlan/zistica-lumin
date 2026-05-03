export { configure, getSDK, setSDK, SynapticSDK } from './sdk.js';
export { trace, span, withSpan } from './trace.js';
export { getCurrentSpan, getCurrentSession } from './context.js';
export { Span } from './span.js';
export { Session, session, withSession } from './session.js';
export { BoundedQueue } from './queue.js';
export { HTTPExporter } from './exporter.js';

export type { SpanData } from './span.js';
export type { SynapticConfig, ResolvedConfig } from './config.js';
export type { Exporter } from './exporter.js';
export type { TraceOptions, SpanHandle } from './trace.js';
export type { SessionOptions } from './session.js';
