// Transformación de datos de la API a lo que se pinta. Las reglas vienen de _docs/api.md:
// null en un conteo es 0, null en un gauge es "sin dato"; conteos con sum_value, gauges con mean_value.
import type { LatestPoint, Series, SeriesResponse } from "../api/client";

export const STALE_AFTER_MS = 2 * 60_000;

export type FlowRow = { minute: string; total: number } & Record<string, number | string>;
export type GaugeRow = { minute: string; value: number | null };

const isCrossings = (s: Series) => s.name === "crossings";
const direction = (labels: Record<string, string>) => labels.direction ?? "";

/** Sentidos conocidos: los de `latest` incluyen series sin cruces en la ventana pedida. */
export function directionsOf(latest: LatestPoint[], series: Series[] = []): string[] {
  const found = new Set<string>();
  for (const p of latest) if (p.name === "crossings") found.add(direction(p.labels));
  for (const s of series) if (isCrossings(s)) found.add(direction(s.labels));
  return [...found].filter(Boolean).sort();
}

/** Minuto i de la rejilla regular de la respuesta: un punto por minuto desde `from`. */
const minuteAt = (from: string, i: number) => new Date(Date.parse(from) + i * 60_000).toISOString();

/** Una fila por minuto con los cruces de cada sentido; el hueco cuenta como 0. */
export function flowRows({ from, series }: SeriesResponse, directions: string[]): FlowRow[] {
  const flow = series.filter(isCrossings);
  const length = flow[0]?.points.length ?? 0;
  return Array.from({ length }, (_, i) => {
    const row: FlowRow = { minute: minuteAt(from, i), total: 0 };
    for (const d of directions) {
      const n = flow
        .filter((s) => direction(s.labels) === d)
        .reduce((sum, s) => sum + (s.points[i]?.sum_value ?? 0), 0);
      row[d] = n;
      row.total += n;
    }
    return row;
  });
}

/** Un gauge por minuto con `mean_value`; sin dato queda `null` (hueco en la gráfica). */
export function gaugeRows({ from, series }: SeriesResponse, name: string): GaugeRow[] {
  const s = series.find((x) => x.name === name);
  return (s?.points ?? []).map((p, i) => ({ minute: minuteAt(from, i), value: p?.mean_value ?? null }));
}

export function dominantDirection(rows: FlowRow[], directions: string[]): string | null {
  let best: string | null = null;
  let max = 0;
  for (const d of directions) {
    const total = rows.reduce((sum, r) => sum + (r[d] as number), 0);
    if (total > max) [best, max] = [d, total];
  }
  return best;
}

export type Kpis = {
  /** Solo las plazas con zona: se escribe "N libres de M monitorizadas", nunca "de la calle". */
  parking: { free: number; total: number } | null;
  /** Cruces en el último minuto con muestras de cualquier serie; null si el proyecto no cuenta. */
  perMinute: number | null;
};

export function kpisOf(latest: LatestPoint[]): Kpis {
  const gauge = (name: string) => latest.find((p) => p.name === name)?.mean_value;
  const free = gauge("spaces_free");
  const total = gauge("spaces_total");
  const flow = latest.filter((p) => p.name === "crossings");
  // `latest` trae el último minuto de cada serie: una dirección sin cruces recientes trae uno viejo.
  const lastMinute = latest.reduce((m, p) => (p.minute > m ? p.minute : m), "");
  // `vehicles_in_scene` la emite el analizador de tráfico aunque no haya cruces: sin cruces es 0, no "no cuenta".
  const countsTraffic = flow.length > 0 || latest.some((p) => p.name === "vehicles_in_scene");
  return {
    parking: free != null && total != null ? { free: Math.round(free), total: Math.round(total) } : null,
    perMinute: countsTraffic
      ? flow.filter((p) => p.minute === lastMinute).reduce((sum, p) => sum + p.sum_value, 0)
      : null,
  };
}

export const parkingText = (p: { free: number; total: number }) =>
  `${p.free} libres de ${p.total} monitorizadas`;

/** No dice si el worker vive (el `status` de la API no es fiable con fuente de fichero, #32). */
export function lastSampleText(lastSeenAt: string | null, now = Date.now()): { text: string; stale: boolean } {
  if (!lastSeenAt) return { text: "sin datos", stale: true };
  const ms = Math.max(0, now - Date.parse(lastSeenAt));
  if (ms > STALE_AFTER_MS) return { text: "sin datos recientes", stale: true };
  const s = Math.round(ms / 1000);
  const hace = s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`;
  return { text: `última muestra hace ${hace}`, stale: false };
}

/** Contrato con el guardián de encuadre (#34): perdido y sin restaurar después. */
export function framingLost(
  lost: { timestamp: string } | null,
  restored: { timestamp: string } | null,
): boolean {
  return !!lost && (!restored || Date.parse(lost.timestamp) > Date.parse(restored.timestamp));
}
