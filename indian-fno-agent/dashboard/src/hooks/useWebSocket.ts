import { useEffect, useRef, useCallback } from 'react';
import { useAppStore } from '../store/useAppStore';

const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws/live`;
const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_ATTEMPTS = 10;

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const { setConnected, addSignal, setPositions } = useAppStore();

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        setConnected(true);
        reconnectAttempts.current = 0;
        console.log('[WS] Connected');
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          switch (msg.event) {
            case 'new_signal':
              addSignal(msg.data);
              break;
            case 'position_update':
              if (Array.isArray(msg.data)) setPositions(msg.data);
              break;
            case 'market_update':
              // Handle price ticks
              break;
            case 'alert':
              // SL/target/kill switch alerts
              console.warn('[WS] Alert:', msg.data);
              break;
            case 'pong':
              break;
            default:
              console.log('[WS] Event:', msg.event);
          }
        } catch (err) {
          console.error('[WS] Parse error:', err);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        console.log('[WS] Disconnected');
        if (reconnectAttempts.current < MAX_RECONNECT_ATTEMPTS) {
          reconnectTimer.current = setTimeout(() => {
            reconnectAttempts.current++;
            connect();
          }, RECONNECT_DELAY * Math.min(reconnectAttempts.current + 1, 5));
        }
      };

      ws.onerror = (err) => {
        console.error('[WS] Error:', err);
        ws.close();
      };

      wsRef.current = ws;
    } catch (err) {
      console.error('[WS] Connection error:', err);
    }
  }, [setConnected, addSignal, setPositions]);

  useEffect(() => {
    connect();

    // Heartbeat ping every 30s
    const heartbeat = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send('ping');
      }
    }, 30000);

    return () => {
      clearInterval(heartbeat);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { isConnected: wsRef.current?.readyState === WebSocket.OPEN };
}
