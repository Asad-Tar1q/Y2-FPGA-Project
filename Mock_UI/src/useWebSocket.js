import { useEffect, useRef, useState, useCallback } from "react";

// Auto-reconnecting websocket hook.
// `url` is a ws:// or wss:// URL. `onMessage` is called with parsed JSON objects.
export function useWebSocket(url, onMessage) {
  const wsRef = useRef(null);
  const onMessageRef = useRef(onMessage);
  const [status, setStatus] = useState("connecting"); // connecting | open | closed

  useEffect(() => { onMessageRef.current = onMessage; }, [onMessage]);

  useEffect(() => {
    let cancelled = false;
    let backoff = 250;
    let reconnectTimer = null;

    function connect() {
      if (cancelled) return;
      setStatus("connecting");
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        backoff = 250;
        setStatus("open");
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          onMessageRef.current?.(msg);
        } catch (e) {
          console.warn("bad JSON from server:", e);
        }
      };
      ws.onclose = () => {
        setStatus("closed");
        if (cancelled) return;
        reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 5000);
      };
      ws.onerror = () => {
        ws.close();
      };
    }

    connect();
    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, [url]);

  const send = useCallback((obj) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }, []);

  return { status, send };
}
