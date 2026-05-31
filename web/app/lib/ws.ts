"use client";

import { useEffect, useRef, useState } from "react";
import type { StreamMessage } from "./types";

// Same-origin WS path; the Next.js dev server proxies /ws/* to the FastAPI
// backend (see next.config.ts). That lets port-forwarding 8088 alone cover
// both HTTP and the live event stream, instead of needing :8503 forwarded too.
const WS_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws/events`
    : "";

/**
 * Subscribe to the live event stream. Auto-reconnects with backoff.
 * Returns the most recent message and a list of recent messages capped at `keep`.
 */
export function useEventStream(keep = 50) {
  const [latest, setLatest] = useState<StreamMessage | null>(null);
  const [recent, setRecent] = useState<StreamMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttempts = useRef(0);

  useEffect(() => {
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        reconnectAttempts.current = 0;
      };
      ws.onclose = () => {
        setConnected(false);
        if (cancelled) return;
        reconnectAttempts.current += 1;
        const backoff = Math.min(8000, 500 * 2 ** reconnectAttempts.current);
        setTimeout(connect, backoff);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data) as StreamMessage;
          setLatest(msg);
          setRecent((prev) => [msg, ...prev].slice(0, keep));
        } catch {
          // ignore malformed payloads
        }
      };
    };

    connect();
    // Keepalive
    const ping = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        try { wsRef.current.send("ping"); } catch { /* noop */ }
      }
    }, 25_000);

    return () => {
      cancelled = true;
      clearInterval(ping);
      wsRef.current?.close();
    };
  }, [keep]);

  return { latest, recent, connected };
}
