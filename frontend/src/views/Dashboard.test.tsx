import { render, screen } from "@testing-library/react";
import { Dashboard } from "./Dashboard";

/** Responde por ruta; lo que no está en `routes` es un 500 de la pasarela. */
function mockApi(routes: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = new URL(url).pathname.replace("/api/v1", "");
    return path in routes
      ? new Response(JSON.stringify(routes[path]), { status: 200 })
      : new Response("bad gateway", { status: 502 });
  }));
}

afterEach(() => vi.unstubAllGlobals());

it("pinta una tarjeta por proyecto con el KPI de aparcamiento y la frescura", async () => {
  const reciente = new Date(Date.now() - 20_000).toISOString();
  mockApi({
    "/projects": { projects: [{ id: "demo", name: "Demo", status: "running", last_seen_at: reciente }] },
    "/projects/demo/metrics/latest": {
      project_id: "demo",
      latest: [
        { minute: reciente, name: "spaces_free", mean_value: 1, sum_value: 0, labels: {} },
        { minute: reciente, name: "spaces_total", mean_value: 3, sum_value: 0, labels: {} },
      ],
    },
  });
  render(<Dashboard />);
  expect(await screen.findByText("1 libres de 3 monitorizadas")).toBeInTheDocument();
  expect(screen.getByText(/última muestra hace/)).toBeInTheDocument();
  expect(screen.getByRole("link")).toHaveAttribute("href", "#/p/demo");
});

it("muestra el estado vacío sin proyectos", async () => {
  mockApi({ "/projects": { projects: [] } });
  render(<Dashboard />);
  expect(await screen.findByText(/Todavía no hay proyectos/)).toBeInTheDocument();
});

it("muestra el error de la API en vez de una lista vacía", async () => {
  mockApi({});
  render(<Dashboard />);
  expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 502");
});
