import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/700.css";
import "@fontsource/inter/800.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";

// Fonts are bundled from npm so renders work offline and are reproducible.
export const sans = "Inter, system-ui, sans-serif";
export const mono = "'JetBrains Mono', ui-monospace, monospace";
export const FPS = 60;
export const W = 1920;
export const H = 1080;

export const c = {
  bg: "#07070b",
  panel: "#0f1117",
  line: "#1f2330",
  fg: "#f4f4f6",
  muted: "#8b8fa3",
  indigo: "#6366f1",
  violet: "#a78bfa",
  teal: "#2dd4bf",
  green: "#4ade80",
  amber: "#fbbf24",
  blue: "#60a5fa",
};

export const gradientText = {
  backgroundImage: `linear-gradient(90deg, ${c.violet}, ${c.indigo} 40%, ${c.teal})`,
  WebkitBackgroundClip: "text",
  backgroundClip: "text",
  color: "transparent",
} as const;
