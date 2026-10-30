import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "./types";

/** The chat panel, kept deliberately plain.
 *
 *  An analyst reading an answer should see the answer. Everything that competed
 *  with it — status badges, tool chips, a "latest tool" strip, a paragraph of
 *  self-description — is gone. What remains is the thread, the citations, and one
 *  input. The badge that survives is the one that changes the meaning of an
 *  answer: whether live traffic is being read.
 */
export function Chat({
  messages,
  busy,
  onSend,
  ready,
  liveGrounding = false,
  suggestions,
}: {
  messages: ChatMessage[];
  traces?: unknown[];
  busy: boolean;
  onSend: (text: string) => void;
  ready: "checking" | "ready" | "error";
  readyMsg?: string;
  liveGrounding?: boolean;
  suggestions: string[];
}) {
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const submit = () => {
    const text = input.trim();
    if (!text || busy) return;
    onSend(text);
    setInput("");
  };

  return (
    <aside className="chat-shell chat-shell--minimal">
      <header className="chat-top">
        <span className="chat-title">Safety assistant</span>
        {liveGrounding && (
          <span className="chat-live" data-chat-live>
            <span className="chat-live-dot" /> live traffic
          </span>
        )}
        {ready === "error" && <span className="chat-warn">offline</span>}
      </header>

      <div className="messages messages--minimal" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <p>Answers come from the files you uploaded and the event store, with citations.</p>
            <div className="chat-starters">
              {suggestions.slice(0, 3).map((suggestion) => (
                <button key={suggestion} type="button" onClick={() => onSend(suggestion)} disabled={busy}>
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) => (
          <Message key={message.id} message={message} />
        ))}

        {busy && (
          <div className="chat-thinking" aria-live="polite">
            <span />
            <span />
            <span />
          </div>
        )}
      </div>

      <div className="chat-composer">
        <textarea
          value={input}
          rows={1}
          onChange={(event) => setInput(event.target.value)}
          placeholder={busy ? "Working…" : "Ask about the files, or about live traffic"}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          disabled={busy}
        />
        <button
          className="chat-send"
          onClick={submit}
          disabled={busy || !input.trim()}
          aria-label="Send"
          title="Send"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="19" x2="12" y2="5" />
            <polyline points="5 12 12 5 19 12" />
          </svg>
        </button>
      </div>
    </aside>
  );
}

function Message({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return <div className="chat-user">{message.text}</div>;
  }
  if (message.role === "error") {
    return <div className="chat-error">{message.text}</div>;
  }

  return (
    <article className="chat-answer">
      <div className="chat-answer-body">
        {message.text ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text}</ReactMarkdown> : <span className="chat-dots">…</span>}
      </div>

      {message.sources && message.sources.length > 0 && (
        <div className="chat-sources" data-message-sources>
          {message.sources.map((source) => (
            <span
              key={`${source.index}-${source.label}`}
              className={`chat-source ${source.kind === "live" ? "is-live" : source.kind === "database" ? "is-db" : ""}`}
              title={source.preview ?? source.label}
            >
              {source.kind === "live" ? "● " : `${source.index} `}
              {source.label}
            </span>
          ))}
        </div>
      )}
    </article>
  );
}
