"use client";

import { useEffect, useRef, useState } from "react";
import type { StreamMessage } from "./types";

// Live event stream. We connect straight to the FastAPI backend's WS port:
// Next.js dev rewrites proxy HTTP but NOT WebSocket upgrades, so a same-origin
// /ws/events through :8088 silently fails to connect (the floorplan then looks
// idle even while the lab is busy). Connecting to the API host:port directly
// sidesteps the broken proxy. Override with NEXT_PUBLIC_WS_URL for other setups
// (a real WS-proxying server, or a remote host); NEXT_PUBLIC_WS_PORT defaults
// to the API's 8503.
const WS_PORT = process.env.NEXT_PUBLIC_WS_PORT || "8503";
const WS_URL =
  typeof window !== "undefined"
    ? process.env.NEXT_PUBLIC_WS_URL ||
      `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.hostname}:${WS_PORT}/ws/events`
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
