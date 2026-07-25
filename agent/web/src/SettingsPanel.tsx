export function SettingsPanel({
  customPrompt,
  theme,
  onPromptChange,
  onThemeToggle,
  onClose,
}: {
  customPrompt: string;
  theme: "light" | "dark";
  onPromptChange: (value: string) => void;
  onThemeToggle: () => void;
  onClose: () => void;
}) {
  return (
    <>
      <div className="panel-overlay" onClick={onClose} />
      <aside className="side-panel settings-panel">
        <div className="side-panel-head">
          <h3>Chat Settings</h3>
          <button className="navbar-btn" onClick={onClose} title="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18" /><path d="M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="side-panel-body">
          <section className="settings-section">
            <div className="settings-row">
              <div>
                <span className="settings-label">Appearance</span>
                <p className="settings-desc">Switch between light and dark mode</p>
              </div>
              <button
                className="theme-toggle"
                onClick={onThemeToggle}
                title={theme === "light" ? "Switch to dark mode" : "Switch to light mode"}
              >
                {theme === "light" ? (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                  </svg>
                ) : (
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="5" /><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
                  </svg>
                )}
                <span>{theme === "light" ? "Dark Mode" : "Light Mode"}</span>
              </button>
            </div>
          </section>

          <section className="settings-section">
            <label className="settings-label" htmlFor="custom-prompt">Custom System Prompt</label>
            <p className="settings-desc">Add instructions the assistant will follow for all queries in this session.</p>
            <textarea
              id="custom-prompt"
              className="settings-textarea"
              value={customPrompt}
              onChange={(e) => onPromptChange(e.target.value)}
              placeholder="e.g. Always cite sources using the occurrence reference number.&#10;e.g. Focus on trend analysis and year-over-year comparisons."
              rows={6}
            />
          </section>
        </div>
      </aside>
    </>
  );
}
