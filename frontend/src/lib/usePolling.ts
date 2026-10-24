import { useEffect, useState } from "react";

export const POLL_MS = 30_000;

export type Polled<T> = { data: T | null; error: Error | null; loading: boolean };

/** fetch + refresco cada 30 s. Ante un fallo conserva el último dato bueno y expone el error. */
export function usePolling<T>(load: () => Promise<T>, deps: unknown[]): Polled<T> {
  const [state, setState] = useState<Polled<T>>({ data: null, error: null, loading: true });

  useEffect(() => {
    let cancelled = false;
    const run = () =>
      load().then(
        (data) => !cancelled && setState({ data, error: null, loading: false }),
        (error: Error) => !cancelled && setState((s) => ({ ...s, error, loading: false })),
      );
    setState({ data: null, error: null, loading: true });
    run();
    const timer = setInterval(run, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
