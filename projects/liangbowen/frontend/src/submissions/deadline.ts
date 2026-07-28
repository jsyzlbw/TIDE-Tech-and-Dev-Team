import { useCallback, useEffect, useState } from "react";

export const DEADLINE_RECHECK_MS = 30_000;
export const MAX_SAFE_TIMER_DELAY = 2_147_483_647;

export function useDeadlineState(dueAt: string) {
  const dueTimestamp = new Date(dueAt).getTime();
  const [expired, setExpired] = useState(() => Date.now() >= dueTimestamp);
  const recheck = useCallback(() => setExpired(Date.now() >= dueTimestamp), [dueTimestamp]);

  useEffect(() => {
    let timeout: ReturnType<typeof setTimeout> | undefined;
    function schedule() {
      const now = Date.now();
      const isExpired = now >= dueTimestamp;
      setExpired(isExpired);
      if (isExpired) {
        timeout = undefined;
        return;
      }
      const remaining = dueTimestamp - now;
      timeout = setTimeout(schedule, Math.min(remaining, DEADLINE_RECHECK_MS, MAX_SAFE_TIMER_DELAY));
    }
    schedule();
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      if (timeout !== undefined) clearTimeout(timeout);
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [dueTimestamp, recheck]);

  return { dueTimestamp, expired, recheck };
}
