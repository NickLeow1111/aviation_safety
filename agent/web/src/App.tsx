import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Chat } from "./Chat";
import { Canvas } from "./Canvas";
import { NavBar } from "./NavBar";
import { HistoryPanel } from "./HistoryPanel";
import { SettingsPanel } from "./SettingsPanel";
import { streamChat } from "./sse";
import type { ChartArtifact, ChatMessage, ChatSession, ToolTrace } from "./types";

type ReadyState = "checking" | "ready" | "error";
type Theme = "light" | "dark";

const STARTER_PROMPTS = [
  "Show me the runway incursion dashboard",
  "Analyze recent bird strike",
  "Audit aircraft maintenance organisation",
  "Build me a 2024 safety intelligence dashboard",
];

const HISTORY_KEY = "sib-chat-history";
const SETTINGS_KEY = "sib-settings";
const MAX_HISTORY = 30;

function parseMaybeJSON(v: unknown): unknown {
  if (typeof v !== "string") return v;
  try { return JSON.parse(v); } catch { return v; }
}

function generateId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function loadHistory(): ChatSession[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function saveHistory(history: ChatSession[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, MAX_HISTORY)));
  } catch { /* quota exceeded — silently ignore */ }
}

function loadSettings(): { customPrompt: string; theme: Theme } {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return { customPrompt: parsed.customPrompt || "", theme: parsed.theme || "light" };
    }
  } catch { /* ignore */ }
  return { customPrompt: "", theme: "light" };
}

function saveSettings(settings: { customPrompt: string; theme: Theme }) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch { /* ignore */ }
}

export default function App() {
  const [ready, setReady] = useState<ReadyState>("checking");
  const [readyMsg, setReadyMsg] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [traces, setTraces] = useState<ToolTrace[]>([]);
  const [charts, setCharts] = useState<ChartArtifact[]>([]);
  const [busy, setBusy] = useState(false);
  const [chatHistory, setChatHistory] = useState<ChatSession[]>(loadHistory);
  const [showHistory, setShowHistory] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [customPrompt, setCustomPrompt] = useState(() => loadSettings().customPrompt);
  const [theme, setTheme] = useState<Theme>(() => loadSettings().theme);
  const sessionId = useRef(`web-${generateId()}`);
  const seqRef = useRef(0);
  const nextId = () => `${Date.now()}-${++seqRef.current}`;

  // Persist settings on change
  useEffect(() => { saveSettings({ customPrompt, theme }); }, [customPrompt, theme]);

  // Apply theme
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  // Persist history on change
  useEffect(() => { saveHistory(chatHistory); }, [chatHistory]);

  // Ready check
  useEffect(() => {
    fetch("/readyz")
      .then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (r.ok) { setReady("ready"); setReadyMsg(body.agent_name ?? "ready"); }
        else { setReady("error"); setReadyMsg(body.detail ?? `HTTP ${r.status}`); }
      })
      .catch((e) => { setReady("error"); setReadyMsg(String(e)); });
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
          setTraces((ts) => [...ts, { id: tid, name: ev.data.name, arguments: ev.data.arguments, status: "running" }]);
          setMessages((m) => m.map((x) => x.id === botId && x.role === "bot" ? { ...x, toolIds: [...x.toolIds, tid] } : x));
        } else if (ev.type === "tool_result") {
          const tid = lastToolIdRef.current;
          const parsed = parseMaybeJSON(ev.data.output);
          setTraces((ts) => ts.map((t) => t.id === tid ? { ...t, output: parsed, status: "done" } : t));
          if ((ev.data.name === "chart_spec" || ev.data.name === "dashboard_spec") && parsed && typeof parsed === "object") {
            setCharts((c) => [...c, { id: nextId(), spec: parsed }]);
          }
        } else if (ev.type === "final") {
          setMessages((m) => m.map((x) => x.id === botId && x.role === "bot" ? { ...x, text: ev.data } : x));
        } else if (ev.type === "error") {
          setMessages((m) => [...m.filter((x) => x.id !== botId), { id: nextId(), role: "error", text: ev.data }]);
        }
      }
    } catch (e) {
      setMessages((m) => [...m.filter((x) => x.id !== botId), { id: nextId(), role: "error", text: String(e) }]);
    } finally {
      setBusy(false);
    }
  };

  const latestUserPrompt = useMemo(
    () => [...messages].reverse().find((m) => m.role === "user")?.text ?? "Awaiting analyst brief",
    [messages],
  );

  // Save current session to history and start fresh
  const handleNewChat = useCallback(() => {
    if (messages.length > 1) {
      const firstUserMsg = messages.find((m) => m.role === "user");
      const lastMsg = messages[messages.length - 1];
      const preview = lastMsg?.role === "bot" ? lastMsg.text.slice(0, 100) : "";
      const session: ChatSession = {
        id: sessionId.current,
        title: firstUserMsg?.text.slice(0, 60) || "New Chat",
        timestamp: Date.now(),
        messageCount: messages.filter((m) => m.role === "user" || m.role === "bot").length,
        preview,
        messages,
        traces,
        charts,
      };
      setChatHistory((prev) => {
        const updated = [session, ...prev];
        return updated.slice(0, MAX_HISTORY);
      });
    }
    setMessages([]);
    setTraces([]);
    setCharts([]);
    sessionId.current = `web-${generateId()}`;
    setShowHistory(false);
    setShowSettings(false);
  }, [messages, traces, charts]);

  // Restore a session from history
  const handleHistorySelect = useCallback((session: ChatSession) => {
    setMessages(session.messages);
    setTraces(session.traces);
    setCharts(session.charts);
    sessionId.current = `web-${generateId()}`;
    setShowHistory(false);
  }, []);

  // Clear all history
  const handleClearHistory = useCallback(() => {
    setChatHistory([]);
  }, []);

  // Toggle theme
  const handleThemeToggle = useCallback(() => {
    setTheme((t) => t === "light" ? "dark" : "light");
  }, []);

  // Close panels on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setShowHistory(false);
        setShowSettings(false);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <div className="app-view">
      <NavBar
        onNewChat={handleNewChat}
        onHistory={() => { setShowHistory((v) => !v); setShowSettings(false); }}
        onSettings={() => { setShowSettings((v) => !v); setShowHistory(false); }}
        historyCount={chatHistory.length}
      />

      {showHistory && (
        <HistoryPanel
          history={chatHistory}
          onSelect={handleHistorySelect}
          onClose={() => setShowHistory(false)}
          onClear={handleClearHistory}
        />
      )}

      {showSettings && (
        <SettingsPanel
          customPrompt={customPrompt}
          theme={theme}
          onPromptChange={setCustomPrompt}
          onThemeToggle={handleThemeToggle}
          onClose={() => setShowSettings(false)}
        />
      )}

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
