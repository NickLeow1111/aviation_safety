import { useRef, useState } from "react";

/** Upload a file of events: preview first, write second.
 *
 *  The preview is not a courtesy. An uploaded coordinate is a claim about where
 *  something happened, and the failure mode we care about is a row that lands
 *  silently at the wrong place. So every row is shown with how its position was
 *  resolved and how its time was read, the rows that cannot be placed are listed
 *  with the reason, and nothing is written until the count is on screen.
 *
 *  The server runs the same parser for both calls, so what the preview promises
 *  is what the import writes.
 */

type PreviewEvent = {
  lat: number;
  lon: number;
  event_time: string;
  event_type: string;
  severity: string;
  place_name: string | null;
  position_source: string | null;
  time_source: string;
  position_note: string | null;
};

type PreviewRow = {
  index: number;
  ok: boolean;
  errors: string[];
  raw: Record<string, string>;
  event: PreviewEvent | null;
};

type PreviewBody = {
  filename: string;
  rows: PreviewRow[];
  summary: {
    total: number;
    ready: number;
    failed: number;
    positions: Record<string, number>;
    time_sources: Record<string, number>;
    types: Record<string, number>;
  };
};

export function UploadPanel({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PreviewBody | null>(null);
  const [busy, setBusy] = useState<"idle" | "preview" | "import">("idle");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  async function choose(selected: File) {
    setFile(selected);
    setPreview(null);
    setResult(null);
    setError(null);
    setBusy("preview");
    const body = new FormData();
    body.append("file", selected);
    try {
      const response = await fetch("/api/events/preview", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? `HTTP ${response.status}`);
      setPreview(payload as PreviewBody);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy("idle");
    }
  }

  async function commit() {
    if (!file) return;
    setBusy("import");
    setError(null);
    const body = new FormData();
    body.append("file", file);
    try {
      const response = await fetch("/api/events/import", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message ?? payload.detail ?? `HTTP ${response.status}`);
      setResult(
        `Imported ${payload.imported} event${payload.imported === 1 ? "" : "s"}` +
          (payload.duplicates_skipped ? `, ${payload.duplicates_skipped} already present` : "") +
          (payload.skipped ? `, ${payload.skipped} row${payload.skipped === 1 ? "" : "s"} skipped` : ""),
      );
      onImported();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy("idle");
    }
  }

  return (
    <div className="upload-overlay" role="dialog" aria-label="Upload safety events">
      <div className="upload-panel">
        <header className="upload-head">
          <div>
            <h3>Upload events</h3>
            <p>CSV with a header row. Coordinates, ICAO/IATA codes and DMS all work in the location column.</p>
          </div>
          <button className="upload-close" onClick={onClose} aria-label="Close upload">
            ×
          </button>
        </header>

        <div
          className="upload-drop"
          data-upload-drop
          onClick={() => inputRef.current?.click()}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            const dropped = event.dataTransfer.files?.[0];
            if (dropped) void choose(dropped);
          }}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".csv,.tsv,.txt"
            data-upload-input
            onChange={(event) => {
              const picked = event.target.files?.[0];
              if (picked) void choose(picked);
            }}
          />
          <strong>{file ? file.name : "Choose a CSV file"}</strong>
          <span>or drop it here · max 5 MB</span>
        </div>

        <p className="upload-hint">
          Columns we read: <code>date</code>, <code>time</code>, <code>location</code> or{" "}
          <code>lat</code>/<code>lon</code>, <code>type</code>, <code>severity</code>,{" "}
          <code>description</code>, <code>registration</code>.
        </p>

        {busy === "preview" && <p className="upload-status">Reading the file…</p>}
        {error && <p className="upload-error">{error}</p>}

        {preview && (
          <>
            <div className="upload-summary" data-upload-summary>
              <span className="upload-pill upload-pill--ok">{preview.summary.ready} ready</span>
              <span className={`upload-pill ${preview.summary.failed ? "upload-pill--bad" : ""}`}>
                {preview.summary.failed} unplaceable
              </span>
              <span className="upload-pill">
                positions:{" "}
                {Object.entries(preview.summary.positions)
                  .map(([key, value]) => `${key} ${value}`)
                  .join(" · ") || "none"}
              </span>
              {preview.summary.time_sources.date_only ? (
                <span className="upload-pill upload-pill--warn">
                  {preview.summary.time_sources.date_only} without a time — recorded at 00:00 UTC
                </span>
              ) : null}
            </div>

            <div className="upload-rows">
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Position</th>
                    <th>How</th>
                    <th>When (UTC)</th>
                    <th>Type</th>
                    <th>Problem</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row) => (
                    <tr key={row.index} data-preview-row={row.index} className={row.ok ? "" : "is-bad"}>
                      <td>{row.index}</td>
                      <td className="mono">
                        {row.event ? `${row.event.lat.toFixed(4)}, ${row.event.lon.toFixed(4)}` : "—"}
                      </td>
                      <td>{row.event?.position_source ?? "—"}</td>
                      <td className="mono">{row.event ? row.event.event_time.replace("T", " ").slice(0, 16) : "—"}</td>
                      <td>{row.event ? row.event.event_type.replace(/_/g, " ") : "—"}</td>
                      <td className="upload-problem">{row.errors.join("; ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <footer className="upload-foot">
              <button className="map-btn map-btn--ghost" onClick={onClose}>
                {result ? "Close" : "Cancel"}
              </button>
              <button
                className="map-btn"
                data-upload-commit
                disabled={busy !== "idle" || preview.summary.ready === 0}
                onClick={() => void commit()}
              >
                {busy === "import" ? "Importing…" : `Import ${preview.summary.ready} event${preview.summary.ready === 1 ? "" : "s"}`}
              </button>
            </footer>
          </>
        )}

        {result && <p className="upload-status upload-status--ok" data-upload-result>{result}</p>}
      </div>
    </div>
  );
}
