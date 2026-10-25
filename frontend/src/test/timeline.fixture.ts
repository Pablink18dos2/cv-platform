// Fixture pequeña con el contrato de /demo/timeline.json (24 s). Los cruces son por segundo.
import type { Timeline } from "../lib/replay";

const free = [3, 3, 3, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 2, 2, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2];
const towardsCamera = [0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0];
const towardsBack = [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0];

export const fixtureTimeline: Timeline = {
  version: 1,
  source: "fixture · clip de prueba",
  duration_s: 24,
  video: "/demo/clip.mp4",
  spaces_total: 3,
  series: free.map((spaces_free, t) => ({
    t,
    spaces_free,
    vehicles_in_scene: (t % 4) + 1,
    crossings: { hacia_camara: towardsCamera[t], hacia_fondo: towardsBack[t] },
  })),
  events: [
    { t: 3, name: "space_occupied", zone: "der-3" },
    { t: 8, name: "space_freed", zone: "der-3" },
    { t: 13, name: "space_occupied", zone: "izq-1" },
    { t: 15, name: "space_occupied", zone: "der-2" },
    { t: 19, name: "space_freed", zone: "izq-1" },
  ],
};
