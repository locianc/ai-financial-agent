import { useEffect, useMemo, useState } from "react";

interface StarSpec {
  left: number;
  top: number;
  size: number;
  delay: number;
  duration: number;
}

interface ParticleSpec {
  left: number;
  size: number;
  delay: number;
  duration: number;
}

function makeStars(count: number): StarSpec[] {
  return Array.from({ length: count }, () => ({
    left: Math.random() * 100,
    top: Math.random() * 100,
    size: 1 + Math.random() * 2.2,
    delay: Math.random() * 3,
    duration: 1.6 + Math.random() * 2.4,
  }));
}

function makeParticles(count: number): ParticleSpec[] {
  return Array.from({ length: count }, () => ({
    left: 8 + Math.random() * 84,
    size: 3 + Math.random() * 5,
    delay: Math.random() * 6,
    duration: 6 + Math.random() * 5,
  }));
}

/** 启动屏：Logo 呼吸发光动效 + 按日/夜切换背景特效，2.5s 后平滑缩小淡出。 */
export default function StartupScreen({
  onFinish,
  isNight,
}: {
  onFinish: () => void;
  isNight: boolean;
}) {
  const [fading, setFading] = useState(false);
  const stars = useMemo(() => makeStars(40), []);
  const particles = useMemo(() => makeParticles(10), []);

  useEffect(() => {
    const t1 = window.setTimeout(() => setFading(true), 2500);
    const t2 = window.setTimeout(onFinish, 3350);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [onFinish]);

  return (
    <div
      className={`fixed inset-0 z-50 flex flex-col items-center justify-center overflow-hidden transition-all duration-[800ms] ${
        isNight ? "bg-ink-950" : "bg-slate-100"
      } ${fading ? "pointer-events-none scale-75 opacity-0" : "scale-100 opacity-100"}`}
    >
      {isNight ? (
        <>
          {stars.map((s, i) => (
            <span
              key={i}
              className="absolute rounded-full bg-white"
              style={{
                left: `${s.left}%`,
                top: `${s.top}%`,
                width: `${s.size}px`,
                height: `${s.size}px`,
                animation: `twinkle ${s.duration}s ease-in-out ${s.delay}s infinite`,
              }}
            />
          ))}
        </>
      ) : (
        <>
          {/* 浅蓝色科技数据网格：静态底网 + 缓慢下移的流动层 */}
          <div
            className="absolute inset-0"
            style={{
              backgroundImage:
                "linear-gradient(rgba(59,130,246,0.12) 1px, transparent 1px), linear-gradient(90deg, rgba(59,130,246,0.12) 1px, transparent 1px)",
              backgroundSize: "48px 48px",
              maskImage: "radial-gradient(ellipse at center, black 25%, transparent 75%)",
              WebkitMaskImage: "radial-gradient(ellipse at center, black 25%, transparent 75%)",
            }}
          />
          <div
            className="absolute inset-0 opacity-60"
            style={{
              backgroundImage:
                "linear-gradient(rgba(59,130,246,0.07) 1px, transparent 1px), linear-gradient(90deg, rgba(59,130,246,0.07) 1px, transparent 1px)",
              backgroundSize: "48px 48px",
              animation: "grid-flow 6s linear infinite",
            }}
          />
          {/* 缓慢上升的浅蓝色光点 */}
          {particles.map((p, i) => (
            <span
              key={i}
              className="absolute bottom-0 rounded-full bg-sky-400/70"
              style={{
                left: `${p.left}%`,
                width: `${p.size}px`,
                height: `${p.size}px`,
                boxShadow: "0 0 8px 2px rgba(56,189,248,0.6)",
                animation: `particle-rise ${p.duration}s linear ${p.delay}s infinite`,
              }}
            />
          ))}
        </>
      )}
      <div className="relative flex flex-col items-center">
        <div style={{ animation: "logo-breathe 2.2s ease-in-out infinite" }}>
          <img
            src="/logo.png"
            className="h-16 w-16 animate-pulse drop-shadow-[0_0_15px_rgba(59,130,246,0.8)]"
            alt="Logo"
          />
        </div>
        <p className="mt-8 text-sm tracking-[0.35em] text-slate-500 dark:text-slate-300">
          Initializing Akshare Core...
        </p>
        <div className="mt-4 h-px w-44 overflow-hidden rounded bg-slate-300 dark:bg-white/10">
          <div
            className="h-full w-full origin-left bg-gradient-to-r from-[#A855F7] via-[#3B82F6] to-[#EC4899]"
            style={{ animation: "loading-bar 1.6s ease-in-out infinite" }}
          />
        </div>
      </div>
    </div>
  );
}
