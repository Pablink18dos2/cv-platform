import { getLatest, listProjects, type ProjectSummary } from "../api/client";
import { Async } from "../components/Async";
import { Freshness } from "../components/Freshness";
import { kpisOf, parkingText } from "../lib/metrics";
import { usePolling } from "../lib/usePolling";

export function Dashboard() {
  const projects = usePolling(listProjects, []);
  return (
    <>
      <h1 className="mb-8 font-serif text-4xl tracking-tight text-ink">Proyectos</h1>
      <Async
        state={projects}
        empty={(p) => p.length === 0}
        emptyText="Todavía no hay proyectos. Arranca uno con `cvp run projects/<id>`."
      >
        {(list) => (
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {list.map((p) => (
              <li key={p.id}>
                <ProjectCard project={p} />
              </li>
            ))}
          </ul>
        )}
      </Async>
    </>
  );
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  return (
    <a
      href={`#/p/${project.id}`}
      className="block rounded-lg border border-line bg-card p-6 transition-colors hover:border-muted"
    >
      <h2 className="font-serif text-2xl text-ink">{project.name}</h2>
      <p className="mb-6 text-sm">
        <Freshness lastSeenAt={project.last_seen_at} />
      </p>
      <CardKpi id={project.id} />
    </a>
  );
}

function CardKpi({ id }: { id: string }) {
  const latest = usePolling(() => getLatest(id), [id]);
  return (
    <Async state={latest} empty={(l) => l.length === 0} emptyText="Sin métricas todavía">
      {(points) => {
        const { parking, perMinute } = kpisOf(points);
        if (parking) return <p className="text-lg text-ink">{parkingText(parking)}</p>;
        if (perMinute !== null) return <p className="text-lg text-ink">{perMinute} vehículos en el último minuto</p>;
        return <p className="text-muted">Sin KPI para este proyecto</p>;
      }}
    </Async>
  );
}
