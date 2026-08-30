import { useState } from "react";
import type { ToolCallState } from "../api/client";

interface ToolTraceProps {
  toolCalls: ToolCallState[];
  streaming: boolean;
}

function formatArgs(args: Record<string, unknown>): string {
  const s = JSON.stringify(args);
  return s.length > 64 ? `${s.slice(0, 61)}…` : s;
}

/** 工具调用轨迹卡片：内嵌在 Assistant 气泡中，展示每个工具的执行状态。 */
export default function ToolTrace({ toolCalls, streaming }: ToolTraceProps) {
  const [open, setOpen] = useState(true);
  if (toolCalls.length === 0) return null;

  const running = toolCalls.some((tc) => tc.status === "running");
  const doneCount = toolCalls.filter((tc) => tc.status !== "running").length;

  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-slate-200 bg-slate-100 text-xs dark:border-white/10 dark:bg-slate-900/70">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-slate-600 transition-colors hover:bg-slate-200 dark:text-slate-300 dark:hover:bg-white/5"
      >
        <span className="text-slate-500">{open ? "▾" : "▸"}</span>
        <span className="font-medium text-slate-800 dark:text-slate-200">
          工具调用 {doneCount}/{toolCalls.length}
        </span>
        {running && (
          <span className="flex items-center gap-1.5 text-[#3B82F6]">
            <span className="inline-block h-2.5 w-2.5 animate-spin rounded-full border-2 border-[#3B82F6] border-t-transparent" />
            AKShare 执行中
          </span>
        )}
        {!running && streaming && <span className="text-slate-500">工具完成，生成回答中…</span>}
      </button>
      {open && (
        <ul className="space-y-1.5 px-3 pb-2.5">
          {toolCalls.map((tc, i) => (
            <li key={i} className="flex items-center gap-2">
              {tc.status === "running" ? (
                <span className="inline-block h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-2 border-[#3B82F6] border-t-transparent" />
              ) : tc.status === "ok" ? (
                <span className="shrink-0 text-emerald-400">✓</span>
              ) : tc.status === "error" ? (
                <span className="shrink-0 text-red-400">✕</span>
              ) : (
                <span className="shrink-0 text-slate-400">■</span>
              )}
              <span className="font-mono text-slate-800 dark:text-slate-200">{tc.tool}</span>
              <span className="truncate text-slate-400">{formatArgs(tc.args)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
