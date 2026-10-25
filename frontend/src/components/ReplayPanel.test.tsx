import { fireEvent, render, screen } from "@testing-library/react";
import { fixtureTimeline } from "../test/timeline.fixture";
import { fetchTimeline } from "../lib/replay";
import { ReplayPanel } from "./ReplayPanel";

beforeEach(() => {
  // jsdom no reproduce vídeo.
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("pinta el vídeo, el rótulo de no-directo, las marcas y el KPI del instante", () => {
  const { container } = render(<ReplayPanel timeline={fixtureTimeline} />);
  const video = container.querySelector("video")!;
  expect(video).toHaveAttribute("src", "/demo/clip.mp4");
  expect(video.loop && video.muted).toBe(true);
  expect(video).not.toHaveAttribute("controls");
  expect(screen.getByText(/no es inferencia en directo/)).toHaveTextContent("fixture · clip de prueba");
  expect(screen.getAllByTitle(/ · /)).toHaveLength(5);
  expect(screen.getByText("3 libres de 3 monitorizadas")).toBeInTheDocument();
});

it("al hacer clic en una marca salta a ese instante y el KPI y el scrub lo siguen", () => {
  const { container } = render(<ReplayPanel timeline={fixtureTimeline} />);
  fireEvent.click(screen.getByTitle("libre · der-3 · 0:08"));
  expect(container.querySelector("video")!.currentTime).toBe(8);
  expect(screen.getByRole("slider", { name: "Posición del vídeo" })).toHaveValue("8");
  expect(screen.getByText("3 libres de 3 monitorizadas")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Cambio anterior" }));
  expect(screen.getByRole("slider")).toHaveValue("3");
  expect(screen.getByText("2 libres de 3 monitorizadas")).toBeInTheDocument();
});

it("el botón de reproducción alterna la etiqueta", () => {
  render(<ReplayPanel timeline={fixtureTimeline} />);
  const play = screen.getByRole("button", { name: "Pausar" });
  fireEvent.pause(document.querySelector("video")!);
  expect(play).toHaveTextContent("Reproducir");
});

describe("fetchTimeline", () => {
  const answer = (res: Response) => vi.stubGlobal("fetch", vi.fn(async () => res));
  it("devuelve la línea de tiempo con un 200 válido", async () => {
    answer(new Response(JSON.stringify(fixtureTimeline), { status: 200 }));
    expect((await fetchTimeline())?.spaces_total).toBe(3);
  });
  it("es null con 404, con HTML de fallback de SPA y sin red", async () => {
    answer(new Response("nope", { status: 404 }));
    expect(await fetchTimeline()).toBeNull();
    answer(new Response("<!doctype html><div id=root>", { status: 200 }));
    expect(await fetchTimeline()).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("offline"))));
    expect(await fetchTimeline()).toBeNull();
  });
});
