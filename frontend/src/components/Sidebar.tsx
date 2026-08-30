import type { SessionInfo } from "../api/client";
import SparkleIcon from "./SparkleIcon";

interface SidebarProps {
  sessions: SessionInfo[];
  activeSessionId: number | null;
  onSelectSession: (id: number) => void;
  onNewChat: () => void;
}

function timeAgo(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

/** 会话侧边栏：新建会话 + GET /sessions 列表（最近活动在前）。 */
export default function Sidebar({ sessions, activeSessionId, onSelectSession, onNewChat }: SidebarProps) {
  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-slate-200 bg-white dark:border-white/10 dark:bg-ink-900">
      <div className="flex items-center gap-2 px-4 py-4">
        <SparkleIcon className="h-6 w-6" />
        <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">AI 投研助手</span>
      </div>
      <div className="px-3 pb-3">
        <button
          onClick={onNewChat}
          className="w-full rounded-lg border border-slate-300 bg-slate-100 px-3 py-2 text-sm text-slate-700 transition-colors hover:bg-slate-200 dark:border-white/10 dark:bg-white/5 dark:text-slate-200 dark:hover:bg-white/10"
        >
          + 新建会话
        </button>
      </div>
      <nav className="flex-1 space-y-1 overflow-y-auto px-2 pb-4">
        {sessions.length === 0 && (
          <p className="px-2 py-4 text-center text-xs text-slate-500">暂无会话，点击上方新建</p>
        )}
        {sessions.map((s) => {
          const active = s.id === activeSessionId;
          return (
            <button
              key={s.id}
              onClick={() => onSelectSession(s.id)}
              className={`w-full rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                active
                  ? "bg-gradient-to-r from-[#A855F7]/20 to-[#3B82F6]/20 text-white ring-1 ring-[#A855F7]/40"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/5"
              }`}
            >
              <div className="truncate">{s.title ?? `会话 ${s.id}`}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">{timeAgo(s.updated_at)}</div>
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
