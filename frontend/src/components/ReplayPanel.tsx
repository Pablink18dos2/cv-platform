import { useEffect, useMemo, useRef, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, XAxis, YAxis } from "recharts";
import { axis, COLORS, Frame } from "./Charts";
import { Notice } from "./Async";
import {
  eventMarks,
  flowWindows,
  kpiAt,
  mmss,
  neighbourChange,
  occupancyRows,
  type Timeline,
} from "../lib/replay";

/** El estado de React solo se mueve cuando el vídeo avanza lo bastante: evita re-pintar las gráficas a 60 fps. */
const MIN_STEP_S = 0.2;
const SKIP_S = 5;

/**
 * Fragmento de vídeo real en bucle con las detecciones ya dibujadas. Las gráficas salen del
 * `timeline.json` y se rellenan hasta la posición del vídeo: no hay inferencia en directo.
 */
export function ReplayPanel({ timeline }: { timeline: Timeline }) {
  const video = useRef<HTMLVideoElement>(null);
  const [time, setTime] = useState(0);
  const [paused, setPaused] = useState(false);
  const [failed, setFailed] = useState(false);
  const duration = timeline.duration_s;
  const marks = useMemo(() => eventMarks(timeline), [timeline]);

  // Sigue la posición real del vídeo, también cuando salta al volver a empezar el bucle.
  useEffect(() => {
    let raf = 0;
    let last = 0;
    const tick = () => {
      const t = video.current?.currentTime ?? 0;
      if (Math.abs(t - last) >= MIN_STEP_S || (t < last && last - t > 0.01)) {
        last = t;
        setTime(t);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  const seek = (t: number) => {
    const to = Math.min(duration, Math.max(0, t));
    if (video.current) video.current.currentTime = to;
    setTime(to);
  };
  const toggle = () => {
    const v = video.current;
    if (!v) return;
    if (v.paused) void v.play()?.catch(() => setPaused(true));
    else v.pause();
  };
  const prev = neighbourChange(timeline.events, time, "prev");
  const next = neighbourChange(timeline.events, time, "next");

  return (
    <section aria-label="Replay del vídeo grabado" className="grid gap-6">
      <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
        <div>
          <video
            ref={video}
            src={timeline.video}
            loop
            muted
            playsInline
            autoPlay
            preload="auto"
            aria-label="Fragmento grabado con detecciones"
            className="aspect-video w-full rounded-lg border border-line bg-card object-contain"
            onLoadedData={(e) => setPaused(e.currentTarget.paused)}
            onPlay={() => setPaused(false)}
            onPause={() => setPaused(true)}
            onError={() => setFailed(true)}
          />
          {failed && <Notice tone="warn">No se pudo cargar el vídeo del replay.</Notice>}
          <p className="mt-2 text-sm text-muted">
            Fragmento grabado, en bucle — no es inferencia en directo
            {timeline.source ? <> · {timeline.source}</> : null}
          </p>
          <div className="mt-4 rounded-lg border border-line bg-card p-4">
            <div className="relative mx-2 h-5" role="group" aria-label="Cambios en las plazas libres">
              {marks.map((m) => (
                <button
                  key={`${m.event.t}-${m.event.zone}-${m.event.name}`}
                  type="button"
                  title={m.title}
                  aria-label={m.title}
                  onClick={() => seek(m.event.t)}
                  style={{ left: `${m.pct}%` }}
                  className="absolute top-0 flex h-5 w-3 -translate-x-1/2 justify-center"
                >
                  <span className={`h-full w-1 rounded-sm ${m.tone === "freed" ? "bg-ok-ink" : "bg-danger-ink"}`} />
                </button>
              ))}
            </div>
            <input
              type="range"
              aria-label="Posición del vídeo"
              aria-valuetext={`${mmss(time)} de ${mmss(duration)}`}
              min={0}
              max={duration}
              step={0.1}
              value={Math.min(time, duration)}
              onChange={(e) => seek(Number(e.target.value))}
              className="block w-full accent-ink"
            />
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Control onClick={toggle} pressed={!paused}>{paused ? "Reproducir" : "Pausar"}</Control>
              <Control onClick={() => seek(time - SKIP_S)}>−{SKIP_S} s</Control>
              <Control onClick={() => seek(time + SKIP_S)}>+{SKIP_S} s</Control>
              <Control onClick={() => prev !== null && seek(prev)} disabled={prev === null}>
                Cambio anterior
              </Control>
              <Control onClick={() => next !== null && seek(next)} disabled={next === null}>
                Cambio siguiente
              </Control>
              <span className="ml-auto text-sm tabular-nums text-muted">
                {mmss(time)} / {mmss(duration)}
              </span>
            </div>
          </div>
        </div>
        <ReplayKpis timeline={timeline} time={time} />
      </div>
      <ReplayCharts timeline={timeline} time={time} />
    </section>
  );
}

function Control({
  children,
  onClick,
  disabled,
  pressed,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  pressed?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={pressed}
      className="rounded-md border border-line bg-card px-3 py-1 text-sm text-ink disabled:text-muted disabled:opacity-60"
    >
      {children}
    </button>
  );
}

function ReplayKpis({ timeline, time }: { timeline: Timeline; time: number }) {
  const { free, total, vehicles } = kpiAt(timeline, time);
  return (
    <dl className="grid content-start gap-4">
      <div className="rounded-lg border border-line bg-card p-5">
        <dt className="text-sm text-muted">Plazas libres</dt>
        <dd className="font-serif text-2xl text-ink">
          {free === null ? "—" : `${free} libres de ${total} monitorizadas`}
        </dd>
      </div>
      <div className="rounded-lg border border-line bg-card p-5">
        <dt className="text-sm text-muted">Vehículos en escena</dt>
        <dd className="font-serif text-2xl text-ink">{vehicles ?? "—"}</dd>
      </div>
    </dl>
  );
}

function ReplayCharts({ timeline, time }: { timeline: Timeline; time: number }) {
  // Las series son de 1 punto por segundo: se recalcula solo al cruzar un punto, no en cada cuarto de segundo.
  const reached = timeline.series.filter((p) => p.t <= time).length;
  const occupancy = useMemo(() => occupancyRows(timeline, time), [timeline, reached]); // eslint-disable-line react-hooks/exhaustive-deps
  const flow = useMemo(() => flowWindows(timeline, time), [timeline, reached]); // eslint-disable-line react-hooks/exhaustive-deps
  const xTicks = { type: "number" as const, domain: [0, timeline.duration_s] as [number, number], tick: axis };
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Frame title="Plazas libres monitorizadas">
        <LineChart data={occupancy}>
          <CartesianGrid stroke="#eaeaea" vertical={false} />
          <XAxis dataKey="t" {...xTicks} tickFormatter={mmss} allowDecimals={false} />
          <YAxis domain={[0, timeline.spaces_total]} allowDecimals={false} tick={axis} width={32} />
          <ReferenceLine x={time} stroke="#111111" strokeDasharray="3 3" />
          <Line
            dataKey="value"
            type="stepAfter"
            stroke={COLORS[0]}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
        </LineChart>
      </Frame>
      <Frame title="Flujo (cruces por ventana de 10 s)">
        <BarChart data={flow.rows}>
          <CartesianGrid stroke="#eaeaea" vertical={false} />
          <XAxis dataKey="label" tick={axis} />
          <YAxis domain={[0, flow.max]} allowDecimals={false} tick={axis} width={32} />
          <Legend />
          {flow.directions.map((d, i) => (
            <Bar key={d} dataKey={d} fill={COLORS[i % COLORS.length]} isAnimationActive={false} />
          ))}
        </BarChart>
      </Frame>
    </div>
  );
}
