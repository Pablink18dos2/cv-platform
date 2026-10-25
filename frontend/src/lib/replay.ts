// Lógica pura del panel de replay (sin DOM): dado el instante `t` del vídeo, qué se ve en las
// gráficas y en los KPI, y dónde caen las marcas de cambio de plazas libres.

export type TimelinePoint = {
  t: number;
  spaces_free: number;
  vehicles_in_scene: number;
  crossings: Record<string, number>;
};
export type TimelineEvent = { t: number; name: "space_freed" | "space_occupied"; zone: string };
export type Timeline = {
  version: 1;
  source: string;
  duration_s: number;
  video: string;
  spaces_total: number;
  series: TimelinePoint[];
  events: TimelineEvent[];
};

/** Ventana de la gráfica de flujo, en segundos. */
export const FLOW_WINDOW_S = 10;
/** Margen para que "anterior/siguiente" no se quede en el cambio donde ya estamos. */
const NEAR_S = 0.25;

/** Valida lo justo para no romper la UI. Un 200 con HTML (fallback de SPA) o cualquier otra cosa es `null`. */
export function parseTimeline(raw: unknown): Timeline | null {
  const x = raw as Partial<Timeline> | null;
  if (!x || typeof x !== "object" || x.version !== 1) return null;
  if (!Array.isArray(x.series) || x.series.length === 0 || !Array.isArray(x.events)) return null;
  if (typeof x.duration_s !== "number" || !(x.duration_s > 0) || typeof x.video !== "string") return null;
  if (typeof x.spaces_total !== "number") return null;
  return { ...x, source: String(x.source ?? "") } as Timeline;
}

export const mmss = (s: number) => {
  const n = Math.max(0, Math.floor(s));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
};

/** Último punto de la serie con `t` menor o igual al instante pedido (null antes del primero). */
export function pointAt(tl: Timeline, t: number): TimelinePoint | null {
  let found: TimelinePoint | null = null;
  for (const p of tl.series) {
    if (p.t > t) break;
    found = p;
  }
  return found;
}

export type ReplayKpi = { free: number | null; total: number; vehicles: number | null };

export function kpiAt(tl: Timeline, t: number): ReplayKpi {
  const p = pointAt(tl, t);
  return { free: p?.spaces_free ?? null, total: tl.spaces_total, vehicles: p?.vehicles_in_scene ?? null };
}

export type OccupancyRow = { t: number; value: number | null };

/** Todos los instantes de la serie; los posteriores a `t` quedan en `null` (hueco), así el eje no se mueve. */
export function occupancyRows(tl: Timeline, t: number): OccupancyRow[] {
  return tl.series.map((p) => ({ t: p.t, value: p.t <= t ? p.spaces_free : null }));
}

export function directionsOfTimeline(tl: Timeline): string[] {
  const found = new Set<string>();
  for (const p of tl.series) for (const d of Object.keys(p.crossings ?? {})) found.add(d);
  return [...found].sort();
}

/**
 * Cruces nuevos de cada punto. Si la serie de un sentido nunca baja se interpreta como contador
 * acumulado (se deriva); si no, como cruces de ese segundo.
 */
function crossingsPerPoint(tl: Timeline, direction: string): number[] {
  const values = tl.series.map((p) => p.crossings?.[direction] ?? 0);
  const cumulative = values.every((v, i) => i === 0 || v >= values[i - 1]);
  return values.map((v, i) => (cumulative ? v - (i === 0 ? 0 : values[i - 1]) : v));
}

export type FlowWindowRow = { t: number; label: string } & Record<string, number | string>;

/**
 * Una fila por ventana de `windowS` segundos con los cruces por sentido contados solo hasta `t`:
 * la ventana en curso crece y las futuras están a 0. `max` es el tope con el vídeo completo
 * (para fijar el eje Y y que no se reescale al avanzar).
 */
export function flowWindows(
  tl: Timeline,
  t: number,
  windowS = FLOW_WINDOW_S,
): { rows: FlowWindowRow[]; directions: string[]; max: number } {
  const directions = directionsOfTimeline(tl);
  const count = Math.max(1, Math.ceil(tl.duration_s / windowS));
  const rows: FlowWindowRow[] = Array.from({ length: count }, (_, w) => {
    const row: FlowWindowRow = { t: w * windowS, label: mmss(w * windowS) };
    for (const d of directions) row[d] = 0;
    return row;
  });
  const full = rows.map(() => 0);
  for (const d of directions) {
    const deltas = crossingsPerPoint(tl, d);
    const sums = rows.map(() => 0);
    tl.series.forEach((p, i) => {
      const w = Math.min(count - 1, Math.floor(p.t / windowS));
      sums[w] += deltas[i];
      if (p.t <= t) (rows[w][d] as number) += deltas[i];
    });
    sums.forEach((s, w) => (full[w] = Math.max(full[w], s)));
  }
  return { rows, directions, max: Math.max(1, ...full) };
}

export type EventMark = { event: TimelineEvent; pct: number; tone: "freed" | "occupied"; title: string };

export const eventWord = (e: TimelineEvent) => (e.name === "space_freed" ? "libre" : "ocupada");

/** Una marca por evento, ordenada por tiempo, con su posición en % de la barra. */
export function eventMarks(tl: Timeline): EventMark[] {
  return [...tl.events]
    .sort((a, b) => a.t - b.t)
    .map((event) => ({
      event,
      pct: Math.min(100, Math.max(0, (event.t / tl.duration_s) * 100)),
      tone: event.name === "space_freed" ? "freed" : "occupied",
      title: `${eventWord(event)} · ${event.zone} · ${mmss(event.t)}`,
    }));
}

/** Instante del cambio siguiente/anterior a `t`; null si no hay (no da la vuelta). */
export function neighbourChange(events: TimelineEvent[], t: number, dir: "next" | "prev"): number | null {
  const times = events.map((e) => e.t).sort((a, b) => a - b);
  if (dir === "next") return times.find((x) => x > t + NEAR_S) ?? null;
  return [...times].reverse().find((x) => x < t - NEAR_S) ?? null;
}

/** `GET /demo/timeline.json`. Cualquier fallo (404, HTML del fallback de SPA, red) es "no hay replay". */
export async function fetchTimeline(url = "/demo/timeline.json"): Promise<Timeline | null> {
  try {
    const res = await fetch(url);
    return res.ok ? parseTimeline(await res.json()) : null;
  } catch {
    return null;
  }
}
