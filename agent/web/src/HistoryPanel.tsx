import type { ChatSession } from "./types";

export function HistoryPanel({
  history,
  onSelect,
  onClose,
  onClear,
}: {
  history: ChatSession[];
  onSelect: (session: ChatSession) => void;
  onClose: () => void;
  onClear: () => void;
}) {
  const sorted = [...history].sort((a, b) => b.timestamp - a.timestamp);

  return (
    <>
      <div className="panel-overlay" onClick={onClose} />
      <aside className="side-panel history-panel">
        <div className="side-panel-head">
          <h3>Chat History</h3>
          <button className="navbar-btn" onClick={onClose} title="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18" /><path d="M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="side-panel-body">
          {sorted.length === 0 ? (
            <div className="panel-empty">
              <p>No chat history yet.</p>
              <p className="panel-empty-hint">Start a conversation and it will appear here when you start a new chat.</p>
            </div>
          ) : (
            <ul className="history-list">
              {sorted.map((session) => (
                <li key={session.id}>
                  <button className="history-item" onClick={() => onSelect(session)}>
                    <div className="history-item-top">
                      <span className="history-item-title">{session.title}</span>
                      <span className="history-item-time">{formatTime(session.timestamp)}</span>
                    </div>
                    <div className="history-item-meta">
                      <span>{session.messageCount} message{session.messageCount !== 1 ? "s" : ""}</span>
                      {session.preview && <span className="history-item-preview">{session.preview}</span>}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {sorted.length > 0 && (
          <div className="side-panel-footer">
            <button className="navbar-btn clear-btn" onClick={onClear}>
              Clear all history
            </button>
          </div>
        )}
      </aside>
    </>
  );
}

function formatTime(ts: number): string {
  const date = new Date(ts);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHrs = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHrs < 24) return `${diffHrs}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString("en-SG", { day: "numeric", month: "short" });
}
