import React from "react";
import {
  AbsoluteFill,
  Audio,
  Easing,
  Img,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { mono, sans } from "./theme";

// Carioca palette, the same as media/brand/build.py: white first, Rio blue, sun yellow,
// sunset orange, palm green; ink for text.
const k = {
  white: "#ffffff",
  blue: "#0070c9",
  sun: "#ffd400",
  sunset: "#ff6b3d",
  palm: "#009c3b",
  ink: "#0b1b2b",
  muted: "#3d5266",
  line: "#d9e2ec",
  sky: "#9fd0f5",
  sunLight: "#fff1a8",
  sunsetLight: "#ffc2a8",
  palmLight: "#a8e6c0",
};
// Roles used by the scenes below.
const c = {
  fg: k.ink,
  muted: k.muted,
  panel: k.white,
  line: k.line,
  agent: k.sunset, // LLM steps
  key: k.blue, // YAML keys, prompts
  done: k.palm,
  cached: k.sun,
  file: [k.blue, k.sunset, k.palm, k.sun],
};
const accentText = { color: k.blue } as const;

// 30 s promo in six scenes, rendered both 16:9 (README, LinkedIn) and 9:16 (Reels, Shorts).
// Layout reads the composition size, so one component serves both.
// Run numbers on screen are sample output, labelled as such; the re-run at $0 is what the
// per-record cache does with unchanged data.
export const PROMO_FPS = 30;
const SCENES = {
  problem: { from: 0, dur: 120 },
  yaml: { from: 120, dur: 180 },
  run: { from: 300, dur: 180 },
  rerun: { from: 480, dur: 180 },
  result: { from: 660, dur: 150 },
  cta: { from: 810, dur: 90 },
};
export const PROMO_FRAMES = SCENES.cta.from + SCENES.cta.dur;

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const ease = Easing.bezier(0.16, 1, 0.3, 1);
const lerp = (f: number, a: number, b: number, from: number, to: number, e = ease) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: e });

const usePortrait = () => {
  const { width, height } = useVideoConfig();
  return height > width;
};

/** Fades and lifts a scene in, fades it out over its last frames. */
const Scene: React.FC<{ dur: number; children: React.ReactNode }> = ({ dur, children }) => {
  const f = useCurrentFrame();
  const o = Math.min(lerp(f, 0, 10, 0, 1), lerp(f, dur - 10, dur, 1, 0));
  const y = lerp(f, 0, 14, 30, 0);
  return (
    <AbsoluteFill style={{ opacity: o, transform: `translateY(${y}px)`, fontFamily: sans, color: c.fg }}>
      {children}
    </AbsoluteFill>
  );
};

const Pop: React.FC<{ at: number; children: React.ReactNode; style?: React.CSSProperties }> = ({ at, children, style }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - at, fps, config: { damping: 13, stiffness: 150 } });
  return (
    <div style={{ opacity: Math.min(1, s * 1.4), transform: `translateY(${(1 - s) * 40}px) scale(${0.9 + s * 0.1})`, ...style }}>
      {children}
    </div>
  );
};

const Caption: React.FC<{ text: React.ReactNode; at?: number }> = ({ text, at = 4 }) => {
  const portrait = usePortrait();
  return (
    <Pop at={at} style={{ fontSize: portrait ? 76 : 70, fontWeight: 800, letterSpacing: -2, textAlign: "center", lineHeight: 1.1 }}>
      {text}
    </Pop>
  );
};

const Window: React.FC<{ title: string; children: React.ReactNode; width: number; style?: React.CSSProperties }> = ({
  title,
  children,
  width,
  style,
}) => (
  <div style={{ width, borderRadius: 22, background: c.panel, border: `1px solid ${c.line}`,
    boxShadow: "0 24px 60px rgba(11,27,43,.12)", overflow: "hidden", ...style }}>
    <div style={{ display: "flex", gap: 10, padding: "16px 22px", borderBottom: `1px solid ${c.line}`, alignItems: "center" }}>
      {["#ff5f56", "#ffbd2e", "#27c93f"].map((col) => <div key={col} style={{ width: 14, height: 14, borderRadius: 7, background: col }} />)}
      <div style={{ marginLeft: 14, color: c.muted, fontFamily: mono, fontSize: 22 }}>{title}</div>
    </div>
    <div style={{ padding: "22px 28px", fontFamily: mono }}>{children}</div>
  </div>
);

/** White with the soft color mesh of the brand images, drifting slowly. */
const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / PROMO_FPS;
  const blob = (x: number, y: number, col: string) => `radial-gradient(40% 35% at ${x}% ${y}%, ${col}cc, transparent 70%)`;
  return (
    <AbsoluteFill style={{ background: k.white }}>
      <AbsoluteFill style={{
        background: [
          blob(4 + Math.sin(t * 0.4) * 6, 10, k.sky),
          blob(38 + Math.cos(t * 0.3) * 8, -6, k.sunLight),
          blob(98, 4 + Math.sin(t * 0.35) * 6, k.sunsetLight),
          blob(100 - Math.cos(t * 0.4) * 5, 96, k.palmLight),
          blob(58, 108, k.sunLight),
          blob(2, 100, k.sky),
        ].join(", "),
      }} />
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 1. the problem
const FILES = [
  { name: "invoice_0412.pdf", tone: k.sunset },
  { name: "invoice_0413.pdf", tone: k.sunset },
  { name: "customers.csv", tone: k.palm },
  { name: "GET /orders?page=1", tone: k.blue },
  { name: "export.xlsx", tone: k.palm },
  { name: "support@inbox", tone: k.blue },
];

const Problem: React.FC = () => {
  const f = useCurrentFrame();
  const portrait = usePortrait();
  const { width, height } = useVideoConfig();
  return (
    <Scene dur={SCENES.problem.dur}>
      {FILES.map((file, i) => {
        // Scattered positions, drifting slowly so the screen feels unsettled.
        const ang = (i / FILES.length) * Math.PI * 2 + 0.4;
        const rx = portrait ? width * 0.3 : width * 0.33;
        const ry = portrait ? height * 0.28 : height * 0.3;
        const x = width / 2 + Math.cos(ang) * rx + Math.sin(f / 20 + i) * 12 - 170;
        const y = height / 2 + Math.sin(ang) * ry + Math.cos(f / 24 + i) * 10 - 36;
        return (
          <div key={file.name} style={{ position: "absolute", left: x, top: y }}>
            <Pop at={4 + i * 6} style={{ width: 340, padding: "18px 22px", borderRadius: 16, background: c.panel,
              border: `2px solid ${file.tone}`, fontFamily: mono, fontSize: 24, color: c.fg,
              transform: `rotate(${(i % 2 ? 1 : -1) * 4}deg)` }}>
              {file.name}
            </Pop>
          </div>
        );
      })}
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center" }}>
        <Caption at={40} text={<>Your data is<br /><span style={{ color: c.muted }}>everywhere.</span></>} />
      </AbsoluteFill>
    </Scene>
  );
};

// ---------------------------------------------------------------- 2. the YAML
const YAML: Array<[string, string?]> = [
  ["name: invoices", "k"],
  ["steps:", "k"],
  ["  - id: read"],
  ["    use: pdf.read"],
  ["    with: { path: invoices/*.pdf }"],
  ["  - id: classify", "a"],
  ["    input: read"],
  ["    agent: Extract the total and", "a"],
  ["      classify the expense.", "a"],
  ["    output:", "a"],
  ["      total: float", "a"],
  ["      category: str", "a"],
  ["  - id: save"],
  ["    use: excel.write"],
];

const Yaml: React.FC = () => {
  const f = useCurrentFrame();
  const portrait = usePortrait();
  const total = YAML.map((l) => l[0]).join("\n").length;
  let left = Math.floor(lerp(f, 20, 150, 0, total, Easing.linear));
  return (
    <Scene dur={SCENES.yaml.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", gap: 50,
        flexDirection: portrait ? "column" : "row" }}>
        <div style={{ width: portrait ? 900 : 620 }}>
          <Caption text={<>Describe the job<br /><span style={accentText}>in YAML.</span></>} />
          <Pop at={30} style={{ marginTop: 30, fontSize: 34, color: c.muted, textAlign: "center" }}>
            Connectors read and write.<br />An LLM step returns typed fields.
          </Pop>
        </div>
        <Window title="pipeline.yaml" width={portrait ? 900 : 760}>
          {YAML.map(([text, tone], i) => {
            const shown = text.slice(0, Math.max(0, left));
            const typing = left >= 0 && left <= text.length;
            left -= text.length + 1;
            const glow = tone === "a" && f > 150 ? 0.5 + 0.5 * Math.sin(f / 6) : 0;
            return (
              <div key={i} style={{ fontSize: 30, lineHeight: 1.6, whiteSpace: "pre", minHeight: 48,
                color: tone === "a" ? c.agent : tone === "k" ? c.key : c.fg,
                background: glow ? `rgba(255,107,61,${0.06 + glow * 0.08})` : undefined }}>
                {shown}
                {typing && <span style={{ background: c.fg, opacity: Math.floor(f / 8) % 2 }}>&nbsp;</span>}
              </div>
            );
          })}
        </Window>
      </AbsoluteFill>
    </Scene>
  );
};

// ---------------------------------------------------------------- 3 and 4. run, then run again
const STEPS = ["read", "classify", "save"];
const RECORDS = 40;

/** Records flow as cards through the three steps; with `cached` every card skips the LLM. */
const Flow: React.FC<{ cached: boolean }> = ({ cached }) => {
  const f = useCurrentFrame();
  const portrait = usePortrait();
  const { width } = useVideoConfig();
  const laneW = portrait ? 760 : 1500;
  const x0 = (width - laneW) / 2;
  const stepX = (i: number) => x0 + (laneW / (STEPS.length - 1)) * i;
  const speed = cached ? 1.8 : 1;
  return (
    <div style={{ position: "relative", height: 260, width }}>
      <div style={{ position: "absolute", left: x0, top: 128, width: laneW, height: 4, background: c.line }} />
      {Array.from({ length: 12 }, (_, n) => {
        const t = ((f * 0.009 * speed + n / 12) % 1);
        const x = x0 + t * laneW;
        const passedAgent = t > 0.5;
        const col = passedAgent ? (cached ? c.cached : c.done) : k.blue;
        return (
          <div key={n} style={{ position: "absolute", left: x - 26, top: 104, width: 52, height: 52, borderRadius: 12,
            background: col, opacity: lerp(f, 10, 30, 0, 0.95), boxShadow: `0 6px 18px ${col}66` }} />
        );
      })}
      {STEPS.map((s, i) => {
        const agent = s === "classify";
        const border = agent ? (cached ? c.cached : c.agent) : c.line;
        return (
          <div key={s} style={{ position: "absolute", left: stepX(i) - 120, top: 70, width: 240, height: 116,
            borderRadius: agent ? 58 : 20, background: c.panel, border: `4px solid ${border}`,
            boxShadow: agent ? `0 0 ${30 + 20 * Math.sin(f / 5)}px ${border}88` : undefined,
            display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", fontFamily: mono }}>
            <div style={{ fontSize: 32, fontWeight: 700 }}>{s}</div>
            <div style={{ fontSize: 20, color: agent && !cached ? border : c.muted }}>
              {agent ? (cached ? "cache hit" : "LLM") : "connector"}
            </div>
          </div>
        );
      })}
    </div>
  );
};

const Meter: React.FC<{ label: string; value: string; tone: string; at: number }> = ({ label, value, tone, at }) => (
  <Pop at={at} style={{ padding: "18px 34px", borderRadius: 18, background: c.panel, border: `1px solid ${c.line}`, textAlign: "center" }}>
    <div style={{ fontSize: 24, color: c.muted }}>{label}</div>
    <div style={{ fontFamily: mono, fontSize: 64, fontWeight: 700, color: tone }}>{value}</div>
  </Pop>
);

const Run: React.FC<{ cached: boolean; dur: number }> = ({ cached, dur }) => {
  const f = useCurrentFrame();
  const portrait = usePortrait();
  const done = Math.round(lerp(f, 20, dur - 40, 0, RECORDS, Easing.out(Easing.quad)));
  const calls = cached ? 0 : done;
  const cost = cached ? 0 : (0.003 * done);
  return (
    <Scene dur={dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", gap: portrait ? 70 : 44, flexDirection: "column" }}>
        <Caption text={cached
          ? <>Run it again?<br /><span style={accentText}>You don't pay twice.</span></>
          : <>Run it.</>} />
        <Window title="terminal" width={portrait ? 900 : 1100}>
          <div style={{ fontSize: 30, color: c.fg }}>
            <span style={{ color: k.blue }}>$</span> agentl run pipeline.yaml
          </div>
          <div style={{ fontSize: 26, color: c.muted, marginTop: 10 }}>
            {cached ? "unchanged records: answers come from the per-record cache" : "classify: calling the LLM per record"}
          </div>
        </Window>
        <Flow cached={cached} />
        <div style={{ display: "flex", gap: 30, flexWrap: "wrap", justifyContent: "center" }}>
          <Meter at={14} label="records" value={`${done}/${RECORDS}`} tone={c.fg} />
          <Meter at={20} label="LLM calls" value={`${calls}`} tone={cached ? k.blue : c.agent} />
          <Meter at={26} label="cost" value={`$${cost.toFixed(2)}`} tone={cached ? c.done : c.agent} />
        </div>
        <div style={{ fontSize: 20, color: c.muted }}>sample run</div>
      </AbsoluteFill>
    </Scene>
  );
};

// ---------------------------------------------------------------- 5. the result
const ROWS = [
  ["0412", "1,240.00", "software"],
  ["0413", "386.50", "travel"],
  ["0414", "92.10", "meals"],
  ["0415", "2,010.00", "hardware"],
];
const FEATURES = ["Cheap model first, strong model when needed", "Resumes where a run failed", "Budget stops the run before it overspends", "Checks before delivering data"];

const Result: React.FC = () => {
  const portrait = usePortrait();
  return (
    <Scene dur={SCENES.result.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", gap: 50, flexDirection: portrait ? "column" : "row" }}>
        <Window title="out/invoices.xlsx" width={portrait ? 900 : 760}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr 1.6fr", fontSize: 28, rowGap: 14 }}>
            {["number", "total", "category"].map((h) => <div key={h} style={{ color: c.muted }}>{h}</div>)}
            {ROWS.map((r, i) => r.map((cell, j) => (
              <Pop key={`${i}${j}`} at={8 + i * 7}><div style={{ color: j === 2 ? c.agent : c.fg }}>{cell}</div></Pop>
            )))}
          </div>
        </Window>
        <div style={{ display: "flex", flexDirection: "column", gap: 20, width: portrait ? 900 : 760 }}>
          {FEATURES.map((t, i) => (
            <Pop key={t} at={30 + i * 12} style={{ fontSize: 36, fontWeight: 600, display: "flex", gap: 18, alignItems: "center" }}>
              <span style={{ color: c.done, fontSize: 40 }}>✓</span>{t}
            </Pop>
          ))}
        </div>
      </AbsoluteFill>
    </Scene>
  );
};

// ---------------------------------------------------------------- 6. call to action
const Cta: React.FC = () => {
  const portrait = usePortrait();
  return (
    <Scene dur={SCENES.cta.dur + 10}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", gap: 40, flexDirection: "column" }}>
        <Pop at={2}><Img src={staticFile("agentl-wordmark-light.svg")} style={{ width: portrait ? 760 : 820 }} /></Pop>
        <Pop at={12} style={{ fontFamily: mono, fontSize: 44, padding: "18px 34px", borderRadius: 16,
          background: c.panel, border: `1px solid ${c.line}` }}>
          <span style={{ color: k.blue }}>$</span> pip install agentl-etl
        </Pop>
        <Pop at={22} style={{ fontSize: 40, color: c.muted }}>agentl.com.br</Pop>
      </AbsoluteFill>
    </Scene>
  );
};

export const Promo: React.FC = () => (
  <AbsoluteFill>
    <Backdrop />
    <Audio src={staticFile("sfx/music.mp3")} volume={(f: number) => 0.22 * interpolate(f, [0, 20, PROMO_FRAMES - 40, PROMO_FRAMES], [0, 1, 1, 0], clamp)} />
    <Sequence from={SCENES.problem.from} durationInFrames={SCENES.problem.dur}><Problem /></Sequence>
    <Sequence from={SCENES.yaml.from} durationInFrames={SCENES.yaml.dur}><Yaml /></Sequence>
    <Sequence from={SCENES.run.from} durationInFrames={SCENES.run.dur}><Run cached={false} dur={SCENES.run.dur} /></Sequence>
    <Sequence from={SCENES.rerun.from} durationInFrames={SCENES.rerun.dur}><Run cached dur={SCENES.rerun.dur} /></Sequence>
    <Sequence from={SCENES.result.from} durationInFrames={SCENES.result.dur}><Result /></Sequence>
    <Sequence from={SCENES.cta.from} durationInFrames={SCENES.cta.dur}><Cta /></Sequence>
  </AbsoluteFill>
);
