import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { FlowRow, GaugeRow } from "../lib/metrics";

export const COLORS = ["#2f3437", "#8fa3b8", "#c9a66b", "#9fb89a"];
export const axis = { fontSize: 12, fill: "#787774" };

const hhmm = (iso: string) =>
  new Date(iso).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });

export function Frame({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <section className="rounded-lg border border-line bg-card p-6">
      <h3 className="mb-4 font-serif text-xl text-ink">{title}</h3>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          {children}
        </ResponsiveContainer>
      </div>
    </section>
  );
}

/** Plazas libres monitorizadas. `value: null` rompe la línea: no se estaba mirando. */
export function OccupancyChart({ rows }: { rows: GaugeRow[] }) {
  return (
    <Frame title="Plazas libres monitorizadas">
      <LineChart data={rows}>
        <CartesianGrid stroke="#eaeaea" vertical={false} />
        <XAxis dataKey="minute" tickFormatter={hhmm} tick={axis} minTickGap={48} />
        <YAxis allowDecimals={false} tick={axis} width={32} />
        <Tooltip labelFormatter={(l) => hhmm(String(l))} formatter={(v) => [v, "libres"]} />
        <Line dataKey="value" stroke={COLORS[0]} dot={rows.filter((r) => r.value !== null).length <= 120} connectNulls={false} isAnimationActive={false} />
      </LineChart>
    </Frame>
  );
}

/** Vehículos por minuto y sentido, apilados. */
export function FlowChart({ rows, directions }: { rows: FlowRow[]; directions: string[] }) {
  return (
    <Frame title="Flujo (vehículos por minuto)">
      <BarChart data={rows}>
        <CartesianGrid stroke="#eaeaea" vertical={false} />
        <XAxis dataKey="minute" tickFormatter={hhmm} tick={axis} minTickGap={48} />
        <YAxis allowDecimals={false} tick={axis} width={32} />
        <Tooltip labelFormatter={(l) => hhmm(String(l))} />
        <Legend />
        {directions.map((d, i) => (
          <Bar key={d} dataKey={d} stackId="flow" fill={COLORS[i % COLORS.length]} isAnimationActive={false} />
        ))}
      </BarChart>
    </Frame>
  );
}
