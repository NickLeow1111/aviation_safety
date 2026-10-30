import { useCallback, useRef, useState } from "react";
import { streamChat } from "./sse";
import type { ChatMessage, SourceRef } from "./types";

let counter = 0;
const nextId = () => `m${++counter}`;

/** The assistant's conversation loop, kept out of the components.
 *
 *  The chat page and the map's ask-panel both need the same streaming behaviour:
 *  append deltas as they arrive, attach the sources at the end, and surface a
 *  failure as a message rather than an empty bubble. One copy of that logic is
 *  enough — two would drift, and the drift would show up as "the sidebar answers
 *  differently from the chat page".
 */
export function useAssistant(getLive: () => { enabled: boolean; bbox: number[] | null }) {
  const sessionId = useRef(`map-${Math.random().toString(36).slice(2)}`);
  const getLiveRef = useRef(getLive);
  getLiveRef.current = getLive;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);

  const send = useCallback(async (text: string) => {
    const question = text.trim();
    if (!question) return;

    const botId = nextId();
    setMessages((current) => [
      ...current,
      { id: nextId(), role: "user", text: question },
      { id: botId, role: "bot", text: "", toolIds: [] },
    ]);
    setBusy(true);

    let sources: SourceRef[] = [];
    const patch = (update: (message: ChatMessage & { role: "bot" }) => ChatMessage) =>
      setMessages((current) => current.map((m) => (m.id === botId && m.role === "bot" ? update(m) : m)));

    try {
      for await (const event of streamChat(sessionId.current, question, undefined, getLiveRef.current())) {
        if (event.type === "sources") {
          sources = event.data;
        } else if (event.type === "delta") {
          patch((m) => ({ ...m, text: m.text + event.data }));
        } else if (event.type === "final") {
          patch((m) => ({ ...m, text: event.data, sources }));
        } else if (event.type === "error") {
          setMessages((current) => [
            ...current.filter((m) => m.id !== botId),
            { id: nextId(), role: "error", text: event.data },
          ]);
        }
      }
    } catch (error) {
      setMessages((current) => [
        ...current.filter((m) => m.id !== botId),
        { id: nextId(), role: "error", text: String(error) },
      ]);
    } finally {
      setBusy(false);
    }
  }, []);

  const reset = useCallback(() => setMessages([]), []);

  return { messages, busy, send, reset };
}
