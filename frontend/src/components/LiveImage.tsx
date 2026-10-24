import { useEffect, useState } from "react";
import { snapshotUrl, streamUrl } from "../api/client";

const SNAPSHOT_EVERY_MS = 2000;
const STREAM_FIRST_FRAME_MS = 3000;

/**
 * MJPEG; si falla o no pinta ningún fotograma en 3 s, snapshots recargados; si tampoco, mensaje.
 * El plazo existe porque con el worker parado el stream no emite y Chromium no pinta la última parte.
 */
export function LiveImage({ projectId }: { projectId: string }) {
  const [mode, setMode] = useState<"stream" | "snapshot" | "none">("stream");
  const [tick, setTick] = useState(0);
  const [streaming, setStreaming] = useState(false);

  useEffect(() => {
    if (mode !== "stream" || streaming) return;
    const timer = setTimeout(() => setMode("snapshot"), STREAM_FIRST_FRAME_MS);
    return () => clearTimeout(timer);
  }, [mode, streaming]);

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
      onLoad={() => setStreaming(true)}
      onError={() => setMode((m) => (m === "stream" ? "snapshot" : "none"))}
    />
  );
}
