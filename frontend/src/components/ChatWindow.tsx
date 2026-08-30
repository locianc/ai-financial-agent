import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchChatStream, fetchRuns, type RunToolCall, type ToolCallState } from "../api/client";
import SparkleIcon from "./SparkleIcon";
import ToolTrace from "./ToolTrace";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  toolCalls?: ToolCallState[];
  error?: string;
  /** 该回答被后端输出合规校验拦截，content 为受限降级答案。 */
  degraded?: boolean;
}

interface ChatWindowProps {
  sessionId: number | null;
  onSessionCreated: (id: number) => void;
  onSessionsChanged: () => void;
}

const SUGGESTIONS = ["分析贵州茅台 600519", "获取宁德时代今日行情", "对比比亚迪与宁德时代估值"];

function parseRunArgs(args: unknown): Record<string, unknown> {
  if (typeof args === "string") {
    try {
      return JSON.parse(args) as Record<string, unknown>;
    } catch {
      return {};
    }
  }
  return (args ?? {}) as Record<string, unknown>;
}

function runToMessage(r: { question: string; answer: string; tool_calls: RunToolCall[]; error: string | null }): ChatMessage[] {
  return [
    { role: "user", content: r.question },
    {
      role: "assistant",
      content: r.answer,
      toolCalls: r.tool_calls.map((tc) => ({
        tool: tc.name,
        args: parseRunArgs(tc.arguments),
        status: "ok",
      })),
      error: r.error ?? undefined,
    },
  ];
}

/** 主聊天区：历史加载 + SSE 流式（工具卡片实时状态 + Markdown 渲染）。 */
export default function ChatWindow({ sessionId, onSessionCreated, onSessionsChanged }: ChatWindowProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const streamIdxRef = useRef(-1);
  const abortRef = useRef<AbortController | null>(null);
  const messagesRef = useRef<ChatMessage[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // 切会话（组件按 key 重挂载）：加载该会话历史；刚发完首轮（null->新 id）不重复加载
  useEffect(() => {
    if (sessionId == null || messagesRef.current.length > 0) return;
    let cancelled = false;
    fetchRuns(sessionId)
      .then((runs) => {
        if (cancelled) return;
        setMessages(runs.flatMap(runToMessage));
      })
      .catch(() => {
        /* 后端未就绪：保持空态 */
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  // 组件卸载（切会话 / 离开页面）：中断在途 SSE 请求，通知后端停止后台推理，防 Token 泄漏
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const patchStreaming = useCallback((fn: (m: ChatMessage) => ChatMessage) => {
    const i = streamIdxRef.current;
    if (i < 0) return;
    setMessages((prev) => prev.map((m, j) => (j === i ? fn(m) : m)));
  }, []);

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    // 正在执行的工具回填为 "stopped"，不再算作 running
    patchStreaming((m) => ({
      ...m,
      toolCalls: (m.toolCalls ?? []).map((tc) =>
        tc.status === "running" ? { ...tc, status: "stopped" } : tc,
      ),
    }));
    setStreaming(false);
  }, [patchStreaming]);

  const handleSend = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;
      setInput("");
      setStreaming(true);
      const base = messagesRef.current.length;
      setMessages((prev) => [
        ...prev,
        { role: "user", content: trimmed },
        { role: "assistant", content: "", toolCalls: [] },
      ]);
      streamIdxRef.current = base + 1;

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        await fetchChatStream(
          trimmed,
          sessionId,
          {
            onToolCall: ({ tool, args }) =>
              patchStreaming((m) => ({
                ...m,
                toolCalls: [...(m.toolCalls ?? []), { tool, args, status: "running" }],
              })),
            onToolResult: ({ tool, status }) =>
              patchStreaming((m) => ({
                ...m,
                toolCalls: (m.toolCalls ?? []).map((tc) =>
                  tc.tool === tool && tc.status === "running" ? { ...tc, status } : tc,
                ),
              })),
            onToken: ({ content }) => patchStreaming((m) => ({ ...m, content: m.content + content })),
            onDone: (done) => {
              if (done.session_id != null && done.session_id !== sessionId) {
                onSessionCreated(done.session_id);
              }
              onSessionsChanged();
              setStreaming(false);
            },
            onError: ({ message }) => {
              patchStreaming((m) => ({ ...m, error: message }));
              setStreaming(false);
            },
            onDegraded: ({ message }) => {
              patchStreaming((m) => ({ ...m, content: message, degraded: true }));
            },
          },
          ctrl.signal,
        );
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          patchStreaming((m) => ({ ...m, error: err instanceof Error ? err.message : String(err) }));
        }
        setStreaming(false);
      }
    },
    [sessionId, streaming, patchStreaming, onSessionCreated, onSessionsChanged],
  );

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void handleSend(input);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto max-w-3xl space-y-6">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <SparkleIcon className="h-12 w-12" />
              <h2 className="mt-4 text-xl font-semibold text-slate-900 dark:text-slate-100">AI 金融投研助手</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">基于 DeepSeek + AKShare 实时数据的智能投研分析</p>
              <div className="mt-6 flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => void handleSend(s)}
                    className="rounded-full border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-600 transition-colors hover:border-[#A855F7]/50 hover:text-slate-900 dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:border-[#A855F7]/40 dark:hover:text-white"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => (
              <div key={i} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
                <div
                  className={
                    m.role === "user"
                      ? "max-w-[80%] rounded-2xl rounded-br-md bg-gradient-to-r from-[#A855F7] to-[#3B82F6] px-4 py-2.5 text-sm text-white"
                      : "max-w-full rounded-2xl rounded-bl-md border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 dark:border-white/10 dark:bg-ink-800/80 dark:text-slate-200"
                  }
                >
                  {m.role === "user" ? (
                    <p className="whitespace-pre-wrap">{m.content}</p>
                  ) : (
                    <>
                      {m.content !== "" ? (
                        <div className="prose prose-sm max-w-none dark:prose-invert">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                        </div>
                      ) : streaming && i === streamIdxRef.current ? (
                        <div className="flex items-center gap-1 py-1.5">
                          {[0, 1, 2].map((d) => (
                            <span
                              key={d}
                              className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400"
                              style={{ animationDelay: `${d * 150}ms` }}
                            />
                          ))}
                        </div>
                      ) : null}
                      {m.degraded && (
                        <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-400/30 dark:bg-amber-500/10 dark:text-amber-300">
                          原始回答因未通过输出合规校验已被系统拦截，以下为受限降级答案
                        </div>
                      )}
                      {m.toolCalls != null && m.toolCalls.length > 0 && (
                        <ToolTrace toolCalls={m.toolCalls} streaming={streaming && i === streamIdxRef.current} />
                      )}
                      {m.error != null && (
                        <div className="mt-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-600 dark:border-red-400/30 dark:bg-red-500/10 dark:text-red-300">
                          {m.error}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="border-t border-slate-200 px-4 py-3 dark:border-white/10">
        <div className="mx-auto flex max-w-3xl items-end gap-2 rounded-xl border border-slate-300 bg-white p-2 focus-within:border-[#3B82F6]/60 dark:border-white/10 dark:bg-ink-800 dark:focus-within:border-[#3B82F6]/50">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            placeholder="输入问题，例如：分析贵州茅台 600519"
            className="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-slate-900 placeholder-slate-400 outline-none dark:text-slate-100 dark:placeholder-slate-500"
          />
          {streaming ? (
            <button
              onClick={handleStop}
              className="rounded-lg bg-red-500 px-4 py-2 text-sm font-medium text-white transition-opacity hover:bg-red-600"
            >
              停止
            </button>
          ) : (
            <button
              onClick={() => void handleSend(input)}
              disabled={input.trim() === ""}
              className="rounded-lg bg-gradient-to-r from-[#A855F7] to-[#3B82F6] px-4 py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-40"
            >
              发送
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
