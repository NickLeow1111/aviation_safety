import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Chat } from "./Chat";
import { Canvas } from "./Canvas";
import { NavBar } from "./NavBar";
import { streamChat } from "./sse";
import type { ChartArtifact, ChatMessage, ToolTrace } from "./types";

type ReadyState = "checking" | "ready" | "error";

const STARTER_PROMPTS = [
  "Show me the runway incursion dashboard",
  "Analyze recent bird strike",
  "Audit aircraft maintenance organisation",
  "Build me a 2024 safety intelligence dashboard",
];

function parseMaybeJSON(v: unknown): unknown {
  if (typeof v !== "string") return v;
  try {
    return JSON.parse(v);
  } catch {
    return v;
  }
}

// crypto.randomUUID() is only defined in secure contexts (HTTPS or http://localhost).
// Fall back to a non-crypto id so the app still works over plain HTTP (e.g. while
// the gateway is on a temporary HTTP listener before TLS is configured).
function generateId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export default function App() {
  const [ready, setReady] = useState<ReadyState>("checking");
  const [readyMsg, setReadyMsg] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [traces, setTraces] = useState<ToolTrace[]>([]);
  const [charts, setCharts] = useState<ChartArtifact[]>([]);
  const [busy, setBusy] = useState(false);
  const sessionId = useRef(`web-${generateId()}`);
  const seqRef = useRef(0);
  const nextId = () => `${Date.now()}-${++seqRef.current}`;

  useEffect(() => {
    fetch("/readyz")
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (r.ok) {
          setReady("ready");
          setReadyMsg(body.agent_name ?? "ready");
        } else {
          setReady("error");
          setReadyMsg(body.detail ?? `HTTP ${r.status}`);
        }
      })
      .catch((e) => {
        setReady("error");
        setReadyMsg(String(e));
      });
  }, []);

  const send = async (text: string) => {
    const userMsg: ChatMessage = { id: nextId(), role: "user", text };
    const botId = nextId();
    const botMsg: ChatMessage = { id: botId, role: "bot", text: "", toolIds: [] };
    setCharts([]);
    setTraces([]);
    setMessages((m) => [...m, userMsg, botMsg]);
    setBusy(true);

    const lastToolIdRef = { current: "" };

    try {
      for await (const ev of streamChat(sessionId.current, text)) {
        if (ev.type === "tool_call") {
          const tid = nextId();
          lastToolIdRef.current = tid;
          setTraces((ts) => [
            ...ts,
            { id: tid, name: ev.data.name, arguments: ev.data.arguments, status: "running" },
          ]);
          setMessages((m) =>
            m.map((x) =>
              x.id === botId && x.role === "bot"
                ? { ...x, toolIds: [...x.toolIds, tid] }
                : x,
            ),
          );
        } else if (ev.type === "tool_result") {
          const tid = lastToolIdRef.current;
          const parsed = parseMaybeJSON(ev.data.output);
          setTraces((ts) =>
            ts.map((t) =>
              t.id === tid ? { ...t, output: parsed, status: "done" } : t,
            ),
          );
          if ((ev.data.name === "chart_spec" || ev.data.name === "dashboard_spec") && parsed && typeof parsed === "object") {
            setCharts((c) => [...c, { id: nextId(), spec: parsed }]);
          }
        } else if (ev.type === "final") {
          setMessages((m) =>
            m.map((x) =>
              x.id === botId && x.role === "bot" ? { ...x, text: ev.data } : x,
            ),
          );
        } else if (ev.type === "error") {
          setMessages((m) => [
            ...m.filter((x) => x.id !== botId),
            { id: nextId(), role: "error", text: ev.data },
          ]);
        }
      }
    } catch (e) {
      setMessages((m) => [
        ...m.filter((x) => x.id !== botId),
        { id: nextId(), role: "error", text: String(e) },
      ]);
    } finally {
      setBusy(false);
    }
  };

  const latestUserPrompt = useMemo(
    () => [...messages].reverse().find((message) => message.role === "user")?.text ?? "Awaiting analyst brief",
    [messages],
  );

  const handleNewChat = useCallback(() => {
    setMessages([]);
    setTraces([]);
    setCharts([]);
    sessionId.current = `web-${generateId()}`;
  }, []);

  return (
    <div className="app-view">
      <NavBar onNewChat={handleNewChat} />
      <div className="app-shell">
        <Chat
          messages={messages}
          traces={traces}
          busy={busy}
          onSend={send}
          ready={ready}
          readyMsg={readyMsg}
          suggestions={STARTER_PROMPTS}
        />

        <main className="workspace single-flow">
          <Canvas
            charts={charts}
            traces={traces}
            lastPrompt={latestUserPrompt}
          />
        </main>
      </div>
    </div>
  );
}
