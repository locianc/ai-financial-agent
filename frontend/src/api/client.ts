// Phase 18B：后端 API 客户端。
// 开发环境经 vite 代理：/api/* -> 后端去前缀；/chat/stream -> 后端原样转发。

export interface SessionInfo {
  id: number;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunToolCall {
  round: number;
  name: string;
  arguments: unknown;
  result: unknown;
}

export interface RunRecord {
  id: number;
  session_id: number | null;
  question: string;
  answer: string;
  tool_calls: RunToolCall[];
  tool_rounds: number;
  max_rounds_reached: boolean;
  error: string | null;
  created_at: string;
}

/** 气泡内工具执行状态（由 SSE 事件增量驱动；"stopped" 为用户主动停止时回填）。 */
export interface ToolCallState {
  tool: string;
  args: Record<string, unknown>;
  status: "running" | "ok" | "error" | "stopped";
}

export interface ToolCallEventPayload {
  tool: string;
  args: Record<string, unknown>;
}

export interface ToolResultEventPayload {
  tool: string;
  status: "ok" | "error";
}

export interface TokenEventPayload {
  content: string;
}

export interface DoneEventPayload {
  session_id: number | null;
  run_id: number | null;
}

export interface ErrorEventPayload {
  message: string;
}

export interface DegradedEventPayload {
  message: string;
  violations: string[];
}

export interface StreamHandlers {
  onToolCall: (payload: ToolCallEventPayload) => void;
  onToolResult: (payload: ToolResultEventPayload) => void;
  onToken: (payload: TokenEventPayload) => void;
  onDone: (payload: DoneEventPayload) => void;
  onError: (payload: ErrorEventPayload) => void;
  onDegraded?: (payload: DegradedEventPayload) => void;
}

async function _getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}: GET ${url}`);
  }
  return (await resp.json()) as T;
}

export async function fetchSessions(): Promise<SessionInfo[]> {
  return _getJson<SessionInfo[]>("/api/sessions");
}

export async function fetchRuns(sessionId: number): Promise<RunRecord[]> {
  return _getJson<RunRecord[]>(`/api/sessions/${sessionId}/runs`);
}

/**
 * POST /chat/stream（SSE）。逐块解析 event:/data: 帧，按类型回调 handlers；
 * 返回 done 事件的载荷（session_id / run_id）。
 */
export async function fetchChatStream(
  message: string,
  sessionId: number | null,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<DoneEventPayload> {
  const resp = await fetch("/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: POST /chat/stream ${body}`);
  }
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent: string | null = null;
  const dataLines: string[] = [];
  let done: DoneEventPayload = { session_id: null, run_id: null };

  const flush = () => {
    if (currentEvent && dataLines.length > 0) {
      const payload = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
      switch (currentEvent) {
        case "tool_call":
          handlers.onToolCall(payload as unknown as ToolCallEventPayload);
          break;
        case "tool_result":
          handlers.onToolResult(payload as unknown as ToolResultEventPayload);
          break;
        case "token":
          handlers.onToken(payload as unknown as TokenEventPayload);
          break;
        case "done":
          done = payload as unknown as DoneEventPayload;
          handlers.onDone(done);
          break;
        case "error":
          handlers.onError(payload as unknown as ErrorEventPayload);
          break;
        case "degraded":
          handlers.onDegraded?.(payload as unknown as DegradedEventPayload);
          break;
      }
    }
    currentEvent = null;
    dataLines.length = 0;
  };

  for (;;) {
    const { value, done: streamDone } = await reader.read();
    if (streamDone) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).replace(/\r$/, "");
      buffer = buffer.slice(idx + 1);
      if (line === "") {
        flush();
      } else if (line.startsWith("event:")) {
        currentEvent = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
  }
  flush();
  return done;
}
