import type { SSEEvent } from "./types";

/**
 * POST to the grounded assistant and yield parsed SSE JSON events.
 * The backend emits `data: {json}\n\n` chunks via FastAPI StreamingResponse.
 */
export async function* streamChat(
  sessionId: string,
  message: string,
  signal?: AbortSignal,
  live?: { enabled: boolean; bbox: number[] | null },
): AsyncGenerator<SSEEvent> {
  const res = await fetch("/api/assistant/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // The map's live state travels with the question, so the assistant grounds on
    // the same traffic picture the user is looking at — and only when that layer
    // is on, because live data costs OpenSky credits.
    body: JSON.stringify({
      session_id: sessionId,
      message,
      live_enabled: Boolean(live?.enabled && live?.bbox),
      bbox: live?.bbox ?? null,
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`assistant failed: ${res.status} ${res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of raw.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          yield JSON.parse(payload) as SSEEvent;
        } catch {
          // ignore malformed chunk
        }
      }
    }
  }
}
