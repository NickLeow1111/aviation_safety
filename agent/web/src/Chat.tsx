import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { ChatMessage, ToolTrace } from "./types";

export function Chat({
  messages,
  traces,
  busy,
  onSend,
  ready,
  readyMsg,
  suggestions,
}: {
  messages: ChatMessage[];
  traces: ToolTrace[];
  busy: boolean;
  onSend: (text: string) => void;
  ready: "checking" | "ready" | "error";
  readyMsg: string;
  suggestions: string[];
}) {
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, traces]);

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    onSend(text);
    setInput("");
  };

  const latestTrace = traces[traces.length - 1];

  return (
    <aside className="chat-shell">
      <div className="chat-header">
        <div>
          <h2>Safety Assistant</h2>
          <p>Synapse-backed analyst copilot for repository, occurrence, and audit follow-up.</p>
        </div>
        <span className={`assistant-state ${ready === "ready" ? "ok" : ready === "error" ? "err" : ""}`}>
          {ready === "checking" ? "Linking" : ready === "ready" ? "Ready" : "Issue"}
        </span>
      </div>

      <div className="assistant-brief">
        <p>
          Ask for a dashboard first, then keep drilling into incidents, findings, documents, and
          occurrence patterns in the same thread.
        </p>
        <small>{readyMsg || "Connection status will appear here."}</small>
      </div>

      <div className="suggestion-row">
        <div className="suggestion-scroll">
          {suggestions.map((suggestion) => (
            <button key={suggestion} type="button" onClick={() => onSend(suggestion)} disabled={busy}>
              {suggestion}
            </button>
          ))}
        </div>
      </div>

      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty">
            Ask for a runway-incursion dashboard, bird-strike review, or any Synapse-backed safety
            drill-down.
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} message={m} traces={traces} />
        ))}
      </div>

      <div className="assistant-footer">
        <div className="footer-label">Latest tool</div>
        <div className="footer-value">
          {latestTrace ? `${latestTrace.name} · ${latestTrace.status}` : "No tool calls yet"}
        </div>
      </div>

      <div className="composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={busy ? "Waiting for response…" : "Ask a question (Shift+Enter for newline)"}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          disabled={busy}
        />
        <button onClick={submit} disabled={busy || !input.trim()}>
          Dispatch
        </button>
      </div>
    </aside>
  );
}

function Message({ message, traces }: { message: ChatMessage; traces: ToolTrace[] }) {
  if (message.role === "user") {
    return <div className="msg user">{message.text}</div>;
  }
  if (message.role === "error") {
    return <div className="msg error">⚠ {message.text}</div>;
  }
  // bot
  const myTraces = traces.filter((t) => message.toolIds.includes(t.id));
  return (
    <article className="message-stack">
      {myTraces.length > 0 && (
        <div className="tool-stack">
          {myTraces.map((t) => (
            <div key={t.id} className={`tool-chip ${t.status === "running" ? "running" : ""}`}>
              <span className="dot" />
              {t.name}
              {t.status === "running" ? "…" : " ✓"}
            </div>
          ))}
        </div>
      )}
      <div className="msg bot">
        {message.text ? (
          <ReactMarkdown>{message.text}</ReactMarkdown>
        ) : (
          <span style={{ color: "var(--muted)" }}>Thinking…</span>
        )}
      </div>
    </article>
  );
}
