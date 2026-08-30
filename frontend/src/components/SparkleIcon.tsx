import { useId } from "react";

/** Gemini 风格四角星：渐变 A855F7 -> 3B82F6 -> EC4899。 */
export default function SparkleIcon({ className }: { className?: string }) {
  const id = useId().replace(/:/g, "sparkle");
  return (
    <svg className={className} viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id={id} x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor="#A855F7" />
          <stop offset="50%" stopColor="#3B82F6" />
          <stop offset="100%" stopColor="#EC4899" />
        </linearGradient>
      </defs>
      <path
        d="M24 2 C25.5 12 33 19.5 46 24 C33 28.5 25.5 36 24 46 C22.5 36 15 28.5 2 24 C15 19.5 22.5 12 24 2 Z"
        fill={`url(#${id})`}
      />
    </svg>
  );
}
