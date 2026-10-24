import { lastSampleText } from "../lib/metrics";

export function Freshness({ lastSeenAt }: { lastSeenAt: string | null }) {
  const { text, stale } = lastSampleText(lastSeenAt);
  return <span className={stale ? "text-warn-ink" : "text-muted"}>{text}</span>;
}
