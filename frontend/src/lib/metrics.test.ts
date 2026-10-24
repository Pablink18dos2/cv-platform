import type { LatestPoint, Series, SeriesResponse } from "../api/client";
import {
  directionsOf,
  dominantDirection,
  flowRows,
  framingLost,
  gaugeRows,
  kpisOf,
  lastSampleText,
  parkingText,
} from "./metrics";

const punto = (minute: string, extra: object = {}) => ({
  minute, samples: 10, seconds: 90, sum_value: 4, mean_value: 2.5,
  min_value: 0, max_value: 5, integral: 150, ...extra,
});
const FROM = "2026-08-24T14:00:00Z";
const serie = (name: string, labels: Record<string, string>, points: Series["points"]): Series => ({
  analyzer: "a", name, labels, points,
});
const respuesta = (series: Series[]): SeriesResponse =>
  ({ project_id: "p", from: FROM, to: "2026-08-24T14:02:00Z", step: "minute", series }) as SeriesResponse;

describe("flowRows", () => {
  const data = respuesta([
    serie("crossings", { direction: "norte" }, [punto("2026-08-24T14:00:00Z", { sum_value: 3 }), null, punto("2026-08-24T14:02:00Z", { sum_value: 1 })]),
  ]);

  it("cuenta el hueco como 0 y usa sum_value, no mean_value", () => {
    const rows = flowRows(data, ["norte"]);
    expect(rows.map((r) => r.norte)).toEqual([3, 0, 1]);
  });

  it("pinta a 0 el sentido que no aparece en la respuesta", () => {
    const rows = flowRows(data, ["norte", "sur"]);
    expect(rows.map((r) => r.sur)).toEqual([0, 0, 0]);
    expect(rows[0].total).toBe(3);
  });

  it("el sentido dominante es el de más cruces en la ventana", () => {
    const rows = flowRows(data, ["norte", "sur"]);
    expect(dominantDirection(rows, ["norte", "sur"])).toBe("norte");
    expect(dominantDirection(flowRows(respuesta([]), ["norte"]), ["norte"])).toBeNull();
  });
});

describe("gaugeRows", () => {
  it("deja hueco (null) donde no hay dato y usa mean_value", () => {
    const data = respuesta([serie("spaces_free", {}, [punto(FROM, { mean_value: 1.5, sum_value: 99 }), null])]);
    expect(gaugeRows(data, "spaces_free").map((r) => r.value)).toEqual([1.5, null]);
  });
});

describe("kpis y direcciones", () => {
  const latest = [
    { minute: "2026-08-24T14:05:00Z", name: "spaces_free", mean_value: 1.2, sum_value: 0, labels: {} },
    { minute: "2026-08-24T14:05:00Z", name: "spaces_total", mean_value: 3, sum_value: 0, labels: {} },
    { minute: "2026-08-24T14:05:00Z", name: "crossings", mean_value: 1, sum_value: 2, labels: { direction: "norte" } },
    { minute: "2026-08-24T13:00:00Z", name: "crossings", mean_value: 1, sum_value: 9, labels: { direction: "sur" } },
  ] as LatestPoint[];

  it("el aparcamiento se escribe 'N libres de M monitorizadas'", () => {
    const { parking } = kpisOf(latest);
    expect(parkingText(parking!)).toBe("1 libres de 3 monitorizadas");
  });

  it("los vehículos por minuto ignoran las series cuyo último minuto es viejo", () => {
    expect(kpisOf(latest).perMinute).toBe(2);
  });

  it("sin cruces pero con vehicles_in_scene el proyecto cuenta tráfico: 0, no null (#36)", () => {
    const sinCruces = [
      { minute: "2026-08-24T14:05:00Z", name: "vehicles_in_scene", mean_value: 4, sum_value: 0, labels: {} },
    ] as LatestPoint[];
    expect(kpisOf(sinCruces).perMinute).toBe(0);
    expect(kpisOf([]).perMinute).toBeNull();
  });

  it("los sentidos salen de latest aunque no cruzaran en la ventana", () => {
    expect(directionsOf(latest)).toEqual(["norte", "sur"]);
  });
});

describe("lastSampleText", () => {
  const now = Date.parse("2026-08-24T14:10:00Z");
  it("dice hace cuánto fue la última muestra", () => {
    expect(lastSampleText("2026-08-24T14:09:30Z", now)).toEqual({ text: "última muestra hace 30 s", stale: false });
  });
  it("pasados 2 min dice 'sin datos recientes' y no afirma nada del worker", () => {
    expect(lastSampleText("2026-08-24T14:07:00Z", now)).toEqual({ text: "sin datos recientes", stale: true });
  });
});

describe("framingLost", () => {
  const t = (timestamp: string) => ({ timestamp });
  it("avisa solo si el último framing_lost es posterior al último framing_restored", () => {
    expect(framingLost(null, null)).toBe(false);
    expect(framingLost(t("2026-08-24T14:00:00Z"), null)).toBe(true);
    expect(framingLost(t("2026-08-24T14:00:00Z"), t("2026-08-24T15:00:00Z"))).toBe(false);
    expect(framingLost(t("2026-08-24T16:00:00Z"), t("2026-08-24T15:00:00Z"))).toBe(true);
  });
});
