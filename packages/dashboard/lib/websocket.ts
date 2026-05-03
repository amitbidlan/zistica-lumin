'use client';

import { useEffect, useRef, useState } from 'react';
import type { Span, Trace } from './api';

export type WSMessage =
  | { type: 'new_span'; trace_id: string; span: Span }
  | { type: 'new_trace'; trace: Trace };

export type ConnectionState = 'connecting' | 'connected' | 'disconnected';

/** Build the WebSocket URL once on the client.
 *
 *  WebSocket cannot go through Next.js `rewrites()` — that proxy is
 *  HTTP-only. So the dashboard connects directly to the API host:port.
 *  In Docker, this requires `-p 8000:8000` to be mapped (the README
 *  documents both ports). When 8000 isn't reachable from the browser,
 *  the connection fails fast and the polling fallback kicks in.
 */
function buildWsUrl(): string | null {
  if (typeof window === 'undefined') return null;
  // Allow override via NEXT_PUBLIC_API_URL (e.g. https://api.example.com)
  const explicit = process.env.NEXT_PUBLIC_API_URL;
  let host: string;
  let scheme: 'ws:' | 'wss:';
  if (explicit && explicit !== '/api') {
    try {
      const u = new URL(explicit);
      host = u.host;
      scheme = u.protocol === 'https:' ? 'wss:' : 'ws:';
    } catch {
      host = `${window.location.hostname}:8000`;
      scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    }
  } else {
    host = `${window.location.hostname}:8000`;
    scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  }
  return `${scheme}//${host}/ws/traces`;
}

/** Subscribe to /ws/traces. Calls `onMessage` for every server message,
 *  reconnects with exponential backoff on disconnect, returns a state
 *  string the UI can render as a connection indicator.
 *
 *  Pattern: latest `onMessage` is held in a ref so callers can pass an
 *  inline closure without restarting the connection on each render.
 */
export function useTraceStream(onMessage: (msg: WSMessage) => void): ConnectionState {
  const [state, setState] = useState<ConnectionState>('connecting');
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const url = buildWsUrl();
    if (!url) return;

    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      setState('connecting');
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }

      ws.onopen = () => {
        if (cancelled) return;
        attempt = 0;
        setState('connected');
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as WSMessage;
          if (msg && (msg.type === 'new_span' || msg.type === 'new_trace')) {
            handlerRef.current(msg);
          }
        } catch {
          // ignore malformed frames
        }
      };

      ws.onerror = () => {
        // onclose will follow; let it handle reconnect
      };

      ws.onclose = () => {
        if (cancelled) return;
        setState('disconnected');
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      // Exponential backoff: 1s, 2s, 4s, 8s, 16s, capped at 30s.
      const delay = Math.min(1000 * 2 ** attempt, 30_000);
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws && ws.readyState !== WebSocket.CLOSED) {
        ws.close();
      }
    };
  }, []);

  return state;
}
