import { useEffect } from "react";

/** Call `tick` every `intervalMs` while `active` is true. */
export function usePolling(tick: () => void, active: boolean, intervalMs = 3000): void {
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(timer);
  }, [tick, active, intervalMs]);
}
