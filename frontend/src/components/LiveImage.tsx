import { useEffect, useState } from "react";
import { snapshotUrl, streamUrl } from "../api/client";

const SNAPSHOT_EVERY_MS = 2000;

/** MJPEG si el navegador lo acepta; si falla, snapshots recargados; si tampoco, mensaje. */
export function LiveImage({ projectId }: { projectId: string }) {
  const [mode, setMode] = useState<"stream" | "snapshot" | "none">("stream");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (mode !== "snapshot") return;
    const timer = setInterval(() => setTick((t) => t + 1), SNAPSHOT_EVERY_MS);
    return () => clearInterval(timer);
  }, [mode]);

  if (mode === "none") {
    return (
      <div className="grid aspect-video place-items-center rounded-lg border border-line bg-card text-muted">
        Sin imagen: el proyecto todavía no ha escrito ningún fotograma.
      </div>
    );
  }
  const src = mode === "stream" ? streamUrl(projectId) : `${snapshotUrl(projectId)}?t=${tick}`;
  return (
    <img
      src={src}
      alt={`Vídeo anotado de ${projectId}`}
      className="aspect-video w-full rounded-lg border border-line bg-card object-contain"
      onError={() => setMode((m) => (m === "stream" ? "snapshot" : "none"))}
    />
  );
}
