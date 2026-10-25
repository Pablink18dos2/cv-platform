import { useEffect, useState } from "react";
import { getLastEvent, getLatest, getMetrics, getProject, type LatestPoint, type SeriesResponse } from "../api/client";
import { Async, Notice } from "../components/Async";
import { FlowChart, OccupancyChart } from "../components/Charts";
import { Freshness } from "../components/Freshness";
import { LiveImage } from "../components/LiveImage";
import { ReplayPanel } from "../components/ReplayPanel";
import {
  directionsOf,
  dominantDirection,
  flowRows,
  framingLost,
  gaugeRows,
  kpisOf,
  parkingText,
} from "../lib/metrics";
import { fetchTimeline, type Timeline } from "../lib/replay";
import { usePolling, type Polled } from "../lib/usePolling";

const WINDOWS = { "1 h": 1, "24 h": 24 } as const;
type WindowLabel = keyof typeof WINDOWS;

/** undefined mientras se consulta; null si el entorno no tiene replay (el resto de entornos devuelve 404). */
function useDemoTimeline(): Timeline | null | undefined {
  const [timeline, setTimeline] = useState<Timeline | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    void fetchTimeline().then((t) => !cancelled && setTimeline(t));
    return () => {
      cancelled = true;
    };
  }, []);
  return timeline;
}

export function ProjectView({ id }: { id: string }) {
  const timeline = useDemoTimeline();
  if (timeline === undefined) return <p className="text-muted" role="status">Cargando…</p>;
  if (timeline) return <ReplayProject id={id} timeline={timeline} />;
  return <LiveProject id={id} />;
}

function ReplayProject({ id, timeline }: { id: string; timeline: Timeline }) {
  const project = usePolling(() => getProject(id), [id]);
  return (
    <>
      <a href="#/" className="text-sm text-muted hover:text-ink">← Proyectos</a>
      <Async state={project} emptyText="">
        {(p) => (
          <header className="mb-8 mt-2">
            <h1 className="font-serif text-4xl tracking-tight text-ink">{p.name}</h1>
          </header>
        )}
      </Async>
      <ReplayPanel timeline={timeline} />
    </>
  );
}

function LiveProject({ id }: { id: string }) {
  const [win, setWin] = useState<WindowLabel>("1 h");
  const project = usePolling(() => getProject(id), [id]);
  const latest = usePolling(() => getLatest(id), [id]);
  const metrics = usePolling(() => {
    const to = new Date();
    return getMetrics(id, new Date(to.getTime() - WINDOWS[win] * 3_600_000), to);
  }, [id, win]);
  const framing = usePolling(
    async () => framingLost(await getLastEvent(id, "framing_lost"), await getLastEvent(id, "framing_restored")),
    [id],
  );

  return (
    <>
      <a href="#/" className="text-sm text-muted hover:text-ink">← Proyectos</a>
      <Async state={project} emptyText="">
        {(p) => (
          <header className="mb-8 mt-2">
            <h1 className="font-serif text-4xl tracking-tight text-ink">{p.name}</h1>
            <p className="text-sm"><Freshness lastSeenAt={p.last_seen_at} /></p>
          </header>
        )}
      </Async>
      {framing.data && (
        <Notice tone="warn">El encuadre de la cámara ha cambiado: las zonas pueden no ser válidas</Notice>
      )}
      <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
        <LiveImage projectId={id} />
        <Kpis latest={latest} metrics={metrics} />
      </div>
      <div className="mb-4 mt-10 flex items-center gap-2">
        <span className="mr-2 text-sm text-muted">Ventana</span>
        {(Object.keys(WINDOWS) as WindowLabel[]).map((w) => (
          <button
            key={w}
            aria-pressed={w === win}
            onClick={() => setWin(w)}
            className={`rounded-md border px-3 py-1 text-sm ${w === win ? "border-ink bg-ink text-white" : "border-line bg-card"}`}
          >
            {w}
          </button>
        ))}
      </div>
      <Charts latest={latest.data ?? []} metrics={metrics} />
    </>
  );
}

function Kpis({ latest, metrics }: { latest: Polled<LatestPoint[]>; metrics: Polled<SeriesResponse> }) {
  return (
    <Async state={latest} emptyText="Este proyecto todavía no ha escrito métricas.">
      {(points) => {
        const { parking, perMinute } = kpisOf(points);
        const directions = directionsOf(points, metrics.data?.series);
        const dominant = metrics.data ? dominantDirection(flowRows(metrics.data, directions), directions) : null;
        return (
          <dl className="grid content-start gap-4">
            <Kpi label="Plazas libres" value={parking ? parkingText(parking) : "No mide aparcamiento"} />
            <Kpi label="Vehículos por minuto" value={perMinute === null ? "No cuenta tráfico" : String(perMinute)} />
            <Kpi label="Sentido dominante" value={dominant ?? "Sin cruces en la ventana"} />
          </dl>
        );
      }}
    </Async>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-card p-5">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="font-serif text-2xl text-ink">{value}</dd>
    </div>
  );
}

function Charts({ latest, metrics }: { latest: LatestPoint[]; metrics: Polled<SeriesResponse> }) {
  return (
    <Async state={metrics} emptyText="">
      {(data) => {
        const directions = directionsOf(latest, data.series);
        const occupancy = gaugeRows(data, "spaces_free");
        const flow = flowRows(data, directions);
        return (
          <div className="grid gap-6 lg:grid-cols-2">
            {occupancy.some((r) => r.value !== null) ? (
              <OccupancyChart rows={occupancy} />
            ) : (
              <EmptyChart title="Plazas libres monitorizadas" text="Sin datos de aparcamiento en esta ventana." />
            )}
            {flow.some((r) => r.total > 0) ? (
              <FlowChart rows={flow} directions={directions} />
            ) : (
              <EmptyChart title="Flujo (vehículos por minuto)" text="Sin cruces en esta ventana." />
            )}
          </div>
        );
      }}
    </Async>
  );
}

function EmptyChart({ title, text }: { title: string; text: string }) {
  return (
    <section className="rounded-lg border border-line bg-card p-6">
      <h3 className="mb-4 font-serif text-xl text-ink">{title}</h3>
      <p className="text-muted">{text}</p>
    </section>
  );
}
