export type SourceRef = {
  index: number;
  kind: string;
  label: string;
  preview?: string;
};

export type SSEEvent =
  | { type: "tool_call"; data: { name: string; arguments: Record<string, unknown> } }
  | { type: "tool_result"; data: { name: string; output: unknown } }
  | { type: "sources"; data: SourceRef[] }
  | { type: "delta"; data: string }
  | { type: "final"; data: string }
  | { type: "error"; data: string };

export type ToolTrace = {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  output?: unknown;
  status: "running" | "done" | "error";
};

export type ChatMessage =
  | { id: string; role: "user"; text: string }
  | { id: string; role: "bot"; text: string; toolIds: string[]; sources?: SourceRef[] }
  | { id: string; role: "error"; text: string };

export type ChartArtifact = {
  id: string;
  spec: any;
};

export type ChatSession = {
  id: string;
  title: string;
  timestamp: number;
  messageCount: number;
  preview: string;
  messages: ChatMessage[];
  traces: ToolTrace[];
  charts: ChartArtifact[];
};

/** What the map's rule controls found on the last live pass.
 *
 *  These used to render underneath the "Apply rules" button, which meant the answer to a
 *  question lived in the middle of the controls that asked it. The Operations board shows
 *  them now, so the map keeps the controls and the board reports the result.
 */
export interface RulesSnapshot {
  at: string;
  summary: string;
  conflicts: Array<{
    icao24_a?: string;
    icao24_b?: string;
    a?: string;
    b?: string;
    separation_nm?: number | null;
    vertical_ft?: number | null;
  }>;
  predicted: Array<{ a?: string; b?: string; predicted_nm?: number | null; time_to_cpa_s?: number | null; current_nm?: number | null }>;
  motion: Array<{ callsign?: string; kind?: string; detail?: string }>;
  unusual: Array<{ callsign?: string; kind?: string; detail?: string }>;
  overspeed: Array<{ icao24?: string; callsign?: string; ground_speed_kt?: number | null; limit_kt?: number | null }>;
}
