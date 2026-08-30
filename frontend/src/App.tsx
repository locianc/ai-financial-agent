import { useCallback, useEffect, useState } from "react";
import { fetchSessions, type SessionInfo } from "./api/client";
import ChatWindow from "./components/ChatWindow";
import Sidebar from "./components/Sidebar";
import StartupScreen from "./components/StartupScreen";

/** 本地时间 6:00-18:00 为白天（亮色），18:00-次日 6:00 为黑夜（dark 模式）。 */
function isNightNow(): boolean {
  const h = new Date().getHours();
  return h >= 18 || h < 6;
}

export default function App() {
  const [booted, setBooted] = useState(false);
  const [isNight, setIsNight] = useState(() => isNightNow());
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  // 会话切换时通过 key 重挂载 ChatWindow（清空消息并加载对应历史）；
  // 首轮流式结束后 null->新 session_id 不回退 key，避免刚生成的对话被清空
  const [conversationKey, setConversationKey] = useState(0);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await fetchSessions());
    } catch {
      // 后端未就绪时保留现有列表
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  // 每分钟按系统本地时间刷新日/夜标记，并同步 <html> 上的 .dark 类，
  // 让 Tailwind 的 dark: 变体作用于全站
  useEffect(() => {
    const apply = () => {
      const night = isNightNow();
      setIsNight(night);
      document.documentElement.classList.toggle("dark", night);
    };
    apply();
    const id = window.setInterval(apply, 60_000);
    return () => window.clearInterval(id);
  }, []);

  const handleNewChat = useCallback(() => {
    setActiveSessionId(null);
    setConversationKey((k) => k + 1);
  }, []);

  const handleSelectSession = useCallback((id: number) => {
    setActiveSessionId(id);
    setConversationKey((k) => k + 1);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden bg-slate-100 text-slate-900 dark:bg-ink-950 dark:text-slate-200">
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={handleSelectSession}
        onNewChat={handleNewChat}
      />
      <main className="flex min-w-0 flex-1 flex-col">
        <ChatWindow
          key={conversationKey}
          sessionId={activeSessionId}
          onSessionCreated={setActiveSessionId}
          onSessionsChanged={() => void refreshSessions()}
        />
      </main>
      {!booted && <StartupScreen isNight={isNight} onFinish={() => setBooted(true)} />}
    </div>
  );
}
