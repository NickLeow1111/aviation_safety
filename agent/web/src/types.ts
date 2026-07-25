export type SSEEvent =
  | { type: "tool_call"; data: { name: string; arguments: Record<string, unknown> } }
  | { type: "tool_result"; data: { name: string; output: unknown } }
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
  | { id: string; role: "bot"; text: string; toolIds: string[] }
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
