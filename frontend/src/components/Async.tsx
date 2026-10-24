import type { ReactNode } from "react";
import type { Polled } from "../lib/usePolling";

/** Los tres estados que toda vista tiene que cubrir: cargando, error y vacío. */
export function Async<T>({
  state,
  empty,
  emptyText,
  children,
}: {
  state: Polled<T>;
  empty?: (data: T) => boolean;
  emptyText: string;
  children: (data: T) => ReactNode;
}) {
  if (state.data === null) {
    if (state.error) return <Notice tone="danger">{state.error.message}</Notice>;
    return <p className="text-muted" role="status">Cargando…</p>;
  }
  if (empty?.(state.data)) return <p className="text-muted">{emptyText}</p>;
  return (
    <>
      {state.error && <Notice tone="warn">No se pudo actualizar: {state.error.message}</Notice>}
      {children(state.data)}
    </>
  );
}

export function Notice({ tone, children }: { tone: "danger" | "warn"; children: ReactNode }) {
  const style = tone === "danger" ? "bg-danger-bg text-danger-ink" : "bg-warn-bg text-warn-ink";
  return (
    <p role="alert" className={`mb-4 rounded-md px-4 py-3 text-sm ${style}`}>
      {children}
    </p>
  );
}
