import { fixtureTimeline as tl } from "../test/timeline.fixture";
import {
  eventMarks,
  flowWindows,
  kpiAt,
  mmss,
  neighbourChange,
  occupancyRows,
  parseTimeline,
  pointAt,
} from "./replay";

describe("parseTimeline", () => {
  it("acepta el contrato y rechaza lo que no lo es", () => {
    expect(parseTimeline(JSON.parse(JSON.stringify(tl)))?.spaces_total).toBe(3);
    expect(parseTimeline(null)).toBeNull();
    expect(parseTimeline("<!doctype html>")).toBeNull();
    expect(parseTimeline({ ...tl, version: 2 })).toBeNull();
    expect(parseTimeline({ ...tl, series: [] })).toBeNull();
  });
});

describe("serie visible", () => {
  it("antes del primer punto no hay KPI; después es el último punto ya alcanzado", () => {
    expect(pointAt(tl, -1)).toBeNull();
    expect(kpiAt(tl, -1)).toEqual({ free: null, total: 3, vehicles: null });
    expect(kpiAt(tl, 3.9)).toEqual({ free: 2, total: 3, vehicles: 4 });
    expect(kpiAt(tl, 999).free).toBe(2);
  });

  it("rellena hasta t y deja el resto vacío, manteniendo el eje completo", () => {
    const rows = occupancyRows(tl, 4.5);
    expect(rows).toHaveLength(25);
    expect(rows.filter((r) => r.value !== null)).toHaveLength(5);
    expect(rows[4]).toEqual({ t: 4, value: 2 });
    expect(rows[5].value).toBeNull();
    // al retroceder se vacía de nuevo
    expect(occupancyRows(tl, 1).filter((r) => r.value !== null)).toHaveLength(2);
  });
});

describe("flowWindows", () => {
  it("agrupa por ventanas de 10 s y revela solo hasta t", () => {
    const end = flowWindows(tl, 24);
    expect(end.directions).toEqual(["hacia_camara", "hacia_fondo"]);
    expect(end.rows.map((r) => r.hacia_camara)).toEqual([3, 1, 1]);
    expect(end.rows.map((r) => r.hacia_fondo)).toEqual([1, 2, 0]);
    const early = flowWindows(tl, 4);
    expect(early.rows[0]).toMatchObject({ hacia_camara: 1, hacia_fondo: 1 });
    expect(early.rows[1]).toMatchObject({ hacia_camara: 0, hacia_fondo: 0 });
    expect(early.max).toBe(end.max);
  });

  it("interpreta como cruces por segundo una serie que no es monótona", () => {
    const perSecond = {
      ...tl,
      series: tl.series.map((p, i) => ({ ...p, crossings: { a: i === 2 || i === 12 ? 1 : 0 } })),
    };
    expect(flowWindows(perSecond, 24).rows.map((r) => r.a)).toEqual([1, 1, 0]);
  });
});

describe("marcas y navegación entre cambios", () => {
  it("una marca por evento con posición, tono y título", () => {
    const marks = eventMarks(tl);
    expect(marks).toHaveLength(5);
    expect(marks[0]).toMatchObject({ tone: "occupied", title: "ocupada · der-3 · 0:03" });
    expect(marks[1]).toMatchObject({ tone: "freed", title: "libre · der-3 · 0:08" });
    expect(marks[1].pct).toBeCloseTo((8 / 24) * 100);
  });

  it("siguiente y anterior no se quedan en el cambio actual ni dan la vuelta", () => {
    expect(neighbourChange(tl.events, 0, "next")).toBe(3);
    expect(neighbourChange(tl.events, 3, "next")).toBe(8);
    expect(neighbourChange(tl.events, 7.9, "prev")).toBe(3);
    expect(neighbourChange(tl.events, 8, "prev")).toBe(3);
    expect(neighbourChange(tl.events, 1, "prev")).toBeNull();
    expect(neighbourChange(tl.events, 19, "next")).toBeNull();
  });
});

it("formatea mm:ss", () => {
  expect([mmss(0), mmss(41.9), mmss(75)]).toEqual(["0:00", "0:41", "1:15"]);
});
