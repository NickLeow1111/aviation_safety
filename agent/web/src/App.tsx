import { useEffect, useMemo, useRef, useState } from "react";
import { Chat } from "./Chat";
import { Canvas } from "./Canvas";
import { streamChat } from "./sse";
import type { ChartArtifact, ChatMessage, ToolTrace } from "./types";

type ReadyState = "checking" | "ready" | "error";
type WorkspaceMode = "repository" | "occurrences" | "aircraft";

const STARTER_PROMPTS = [
  "Show me the runway incursion dashboard",
  "Analyze recent bird strike",
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

function isTableArtifact(spec: unknown): spec is { type: "table"; rows?: unknown[] } {
  return Boolean(spec && typeof spec === "object" && (spec as { type?: string }).type === "table");
}

function countArtifactRows(charts: ChartArtifact[]): number {
  return charts.reduce((total, chart) => {
    if (isTableArtifact(chart.spec)) {
      return total + (Array.isArray(chart.spec.rows) ? chart.spec.rows.length : 0);
    }
    const values = (chart.spec as { data?: { values?: unknown[] } })?.data?.values;
    return total + (Array.isArray(values) ? values.length : 0);
  }, 0);
}

function inferArtifactTitle(spec: unknown, index: number): string {
  if (spec && typeof spec === "object" && typeof (spec as { title?: unknown }).title === "string") {
    return (spec as { title: string }).title;
  }
  if (isTableArtifact(spec)) return `Table ${index + 1}`;
  return `Visual ${index + 1}`;
}

function extractHighlights(text: string, charts: ChartArtifact[]): string[] {
  const cleaned = text
    .split("\n")
    .map((line) => line.replace(/^[-*\d.\s]+/, "").trim())
    .filter((line) => line && !line.toLowerCase().startsWith("sources:"));

  if (cleaned.length > 0) {
    return cleaned.slice(0, 3);
  }

  if (charts.length > 0) {
    return charts
      .slice(-3)
      .reverse()
      .map((chart, index) => `${inferArtifactTitle(chart.spec, charts.length - 1 - index)} ready for review.`);
  }

  return [
    "Start with a dashboard prompt to populate the operational picture.",
    "Use the assistant rail to stack follow-up questions against the same session.",
    "Tool traces refresh per prompt so the current run is easier to inspect.",
  ];
}

function inferWorkspaceMode(prompt: string, charts: ChartArtifact[]): WorkspaceMode {
  const chartHints = charts
    .map((chart) => {
      const spec = chart.spec as { title?: string; domain?: string };
      return [spec?.title, spec?.domain].filter(Boolean).join(" ");
    })
    .join(" ")
    .toLowerCase();
  const haystack = `${prompt} ${chartHints}`.toLowerCase();

  if (/(bird|runway|incursion|occurrence|occurrences|surveillance|finding|findings|audit|safety intelligence)/.test(haystack)) {
    return "occurrences";
  }
  if (/(document|documents|repository|manual|guidance|change mgmt|reference)/.test(haystack)) {
    return "repository";
  }
  if (/(aircraft|tail|callsign|track|tracks|flight|fleet|360|vector)/.test(haystack)) {
    return "aircraft";
  }
  return "occurrences";
}

function StatCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="stat-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

export default function App() {
  const [ready, setReady] = useState<ReadyState>("checking");
  const [readyMsg, setReadyMsg] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [traces, setTraces] = useState<ToolTrace[]>([]);
  const [charts, setCharts] = useState<ChartArtifact[]>([]);
  const [busy, setBusy] = useState(false);
  const sessionId = useMemo(() => `web-${generateId()}`, []);
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
      for await (const ev of streamChat(sessionId, text)) {
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
  const latestNarrative = useMemo(
    () => [...messages].reverse().find((message) => message.role === "bot")?.text ?? "",
    [messages],
  );
  const chartCount = charts.filter((chart) => !isTableArtifact(chart.spec)).length;
  const tableCount = charts.length - chartCount;
  const recordCount = countArtifactRows(charts);
  const recentTitles = charts
    .slice(-4)
    .reverse()
    .map((chart, index) => inferArtifactTitle(chart.spec, charts.length - 1 - index));
  const highlights = useMemo(() => extractHighlights(latestNarrative, charts), [latestNarrative, charts]);
  const workspaceMode = useMemo(() => inferWorkspaceMode(latestUserPrompt, charts), [latestUserPrompt, charts]);

  // Keep the right-hand workspace blank until the analyst starts a conversation,
  // so first launch shows only the assistant chat on the left.
  const hasStarted = messages.length > 0 || charts.length > 0;

  return (
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

      <main className="workspace">
        {hasStarted && (
          <>
            <header className="workspace-header">
              <div>
                <span className="workspace-kicker">Aviation Safety Risk Dashboard</span>
                <h1>Aviation Safety Intelligence</h1>
                <p>
                  A cleaner analyst workspace for chaining Synapse-backed questions into a single
                  operational picture.
                </p>
              </div>

              <div className="workspace-controls">
                <span className={`status-pill ${ready === "ready" ? "ok" : ready === "error" ? "err" : ""}`}>
                  {ready === "checking" ? "Link check" : ready === "ready" ? readyMsg : readyMsg || "Unavailable"}
                </span>
                <div className="mode-pills" aria-hidden="true">
                  <span className={workspaceMode === "repository" ? "active" : ""}>Repository</span>
                  <span className={workspaceMode === "occurrences" ? "active" : ""}>Occurrences</span>
                  <span className={workspaceMode === "aircraft" ? "active" : ""}>Aircraft 360</span>
                </div>
              </div>
            </header>

            <section className="hero-banner">
              <div className="hero-copy">
                <span className="eyebrow">Live brief</span>
                <h2>{latestUserPrompt}</h2>
                <p>
                  The workspace now prioritises a single spotlight visualization, a compact
                  intelligence rail, and a lower analytics deck so follow-up questions feel cumulative
                  instead of stacked like chat logs.
                </p>
              </div>

              <div className="hero-cards">
                <div className="hero-note">
                  <span>Mission profile</span>
                  <strong>{charts.length > 0 ? "Multi-panel analysis" : "Waiting for first analysis"}</strong>
                  <small>{busy ? "Agent is assembling the next view." : "Ask for a dashboard or a focused drill-down."}</small>
                </div>
                <div className="hero-note accent">
                  <span>Recent outputs</span>
                  <strong>{recentTitles[0] ?? "No visuals yet"}</strong>
                  <small>{recentTitles[1] ?? "Only the current prompt's artifacts stay on the canvas."}</small>
                </div>
              </div>
            </section>

            <section className="stats-strip">
              <StatCard
                label="Visuals"
                value={String(charts.length).padStart(2, "0")}
                detail={`${chartCount} charts · ${tableCount} tables`}
              />
              <StatCard
                label="Records in view"
                value={String(recordCount).padStart(2, "0")}
                detail="Estimated from rendered artifacts"
              />
              <StatCard
                label="Tool activity"
                value={String(traces.length).padStart(2, "0")}
                detail={busy ? "Current run in progress" : "Function calls for this prompt"}
              />
              <StatCard
                label="Assistant memo"
                value={highlights.length > 0 ? String(highlights.length).padStart(2, "0") : "00"}
                detail="Key takeaways pinned to the right rail"
              />
            </section>

            <Canvas
              charts={charts}
              traces={traces}
              summaryText={latestNarrative}
              lastPrompt={latestUserPrompt}
              highlights={highlights}
            />
          </>
        )}
      </main>
    </div>
  );
}
