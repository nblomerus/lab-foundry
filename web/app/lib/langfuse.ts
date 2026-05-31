/** Build a Langfuse trace URL. Works for cloud and self-host. */
export function traceUrl(host: string | null, traceId: string | null): string | null {
  if (!host || !traceId) return null;
  const base = host.replace(/\/$/, "");
  return `${base}/trace/${traceId}`;
}
