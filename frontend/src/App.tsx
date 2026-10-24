import { useEffect, useState } from "react";
import { Dashboard } from "./views/Dashboard";
import { ProjectView } from "./views/ProjectView";

/** Dos vistas: `#/` (dashboard) y `#/p/<id>` (detalle). Sin librería de rutas. */
function useHash() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => setHash(location.hash);
    addEventListener("hashchange", onChange);
    return () => removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export default function App() {
  const projectId = /^#\/p\/(.+)$/.exec(useHash())?.[1];
  return (
    <main className="mx-auto max-w-6xl px-6 py-12">
      {projectId ? <ProjectView id={decodeURIComponent(projectId)} /> : <Dashboard />}
    </main>
  );
}
