// Único punto de contacto con la API. Los tipos salen de openapi.yaml (`npm run gen:api`).
import type { components } from "./schema";

type Schemas = components["schemas"];
export type ProjectSummary = Schemas["ProjectSummary"];
export type LatestPoint = Schemas["LatestPoint"];
export type Series = Schemas["Series"];
export type SeriesResponse = Schemas["SeriesResponse"];
export type EventOut = Schemas["EventOut"];

const BASE = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const API = `${BASE}/api/v1`;

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const query = new URLSearchParams(params).toString();
  let res: Response;
  try {
    res = await fetch(`${API}${path}${query ? `?${query}` : ""}`);
  } catch {
    throw new ApiError("network_error", `No se pudo conectar con la API en ${BASE}`);
  }
  if (!res.ok) {
    // La API devuelve siempre { error: { code, message } }; una pasarela intermedia, no.
    const body = await res.json().catch(() => null);
    throw new ApiError(body?.error?.code ?? "http_error", body?.error?.message ?? `HTTP ${res.status}`);
  }
  return res.json();
}

const project = (id: string) => `/projects/${encodeURIComponent(id)}`;

export const listProjects = () =>
  get<Schemas["ProjectList"]>("/projects").then((r) => r.projects);

export const getProject = (id: string) => get<Schemas["ProjectDetail"]>(project(id));

export const getLatest = (id: string) =>
  get<Schemas["LatestResponse"]>(`${project(id)}/metrics/latest`).then((r) => r.latest);

export const getMetrics = (id: string, from: Date, to: Date) =>
  get<SeriesResponse>(`${project(id)}/metrics`, {
    from: from.toISOString(),
    to: to.toISOString(),
  });

export const getLastEvent = (id: string, name: string) =>
  get<Schemas["EventsResponse"]>(`${project(id)}/events`, { name, limit: "1" }).then(
    (r) => r.events[0] ?? null,
  );

export const streamUrl = (id: string) => `${API}${project(id)}/stream.mjpg`;
export const snapshotUrl = (id: string) => `${API}${project(id)}/snapshot.jpg`;
