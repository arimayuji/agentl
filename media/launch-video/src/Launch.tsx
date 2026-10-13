import React from "react";
import {
  AbsoluteFill,
  Easing,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { c, gradientText, mono, sans } from "./theme";

// ---------------------------------------------------------------- timeline
const S = {
  hook: { from: 0, dur: 270 },
  question: { from: 270, dur: 240 },
  logo: { from: 510, dur: 300 },
  pipeline: { from: 810, dur: 1200 },
  notify: { from: 2010, dur: 360 },
  features: { from: 2370, dur: 390 },
  cta: { from: 2760, dur: 360 },
};
export const LAUNCH_FRAMES = S.cta.from + S.cta.dur;

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;

/** Fade a whole scene in and out so cuts never feel abrupt. */
const SceneFade: React.FC<{ dur: number; children: React.ReactNode }> = ({ dur, children }) => {
  const f = useCurrentFrame();
  const o = interpolate(f, [0, 18, dur - 18, dur], [0, 1, 1, 0], clamp);
  const s = interpolate(f, [0, dur], [1.0, 1.03]);
  return <AbsoluteFill style={{ opacity: o, transform: `scale(${s})` }}>{children}</AbsoluteFill>;
};

/** A word that rises and un-blurs into place. */
const Rise: React.FC<{ at: number; children: React.ReactNode; style?: React.CSSProperties }> = ({
  at,
  children,
  style,
}) => {
  const f = useCurrentFrame();
  const p = interpolate(f, [at, at + 30], [0, 1], { ...clamp, easing: ease });
  return (
    <span
      style={{
        display: "inline-block",
        opacity: p,
        transform: `translateY(${(1 - p) * 40}px)`,
        filter: `blur(${(1 - p) * 12}px)`,
        ...style,
      }}
    >
      {children}
    </span>
  );
};

// ---------------------------------------------------------------- background
const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / 60;
  const g1x = 30 + Math.sin(t * 0.35) * 12;
  const g2x = 70 + Math.cos(t * 0.3) * 12;
  return (
    <AbsoluteFill style={{ background: c.bg, overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(900px 600px at ${g1x}% 30%, rgba(99,102,241,.22), transparent 70%),
                       radial-gradient(800px 600px at ${g2x}% 75%, rgba(45,212,191,.14), transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage: `linear-gradient(${c.line}55 1px, transparent 1px), linear-gradient(90deg, ${c.line}55 1px, transparent 1px)`,
          backgroundSize: "80px 80px",
          backgroundPosition: `0 ${-(f * 0.3) % 80}px`,
          maskImage: "radial-gradient(ellipse at center, black 30%, transparent 75%)",
        }}
      />
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 1. hook
const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const strike = interpolate(f, [150, 185], [0, 1], { ...clamp, easing: ease });
  return (
    <SceneFade dur={S.hook.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
        <div style={{ fontSize: 110, fontWeight: 800, color: c.fg, letterSpacing: -3, textAlign: "center", lineHeight: 1.1 }}>
          <Rise at={10}>Data pipelines</Rise>{" "}
          <Rise at={40}>are still</Rise>
          <br />
          <span style={{ position: "relative", display: "inline-block" }}>
            <Rise at={80} style={{ color: c.muted }}>
              glue code.
            </Rise>
            <span
              style={{
                position: "absolute",
                left: 0,
                top: "54%",
                height: 10,
                width: `${strike * 100}%`,
                background: c.amber,
                borderRadius: 6,
              }}
            />
          </span>
        </div>
      </AbsoluteFill>
    </SceneFade>
  );
};

// ---------------------------------------------------------------- 2. question
const Question: React.FC = () => (
  <SceneFade dur={S.question.dur}>
    <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
      <div style={{ fontSize: 104, fontWeight: 800, letterSpacing: -3, textAlign: "center", lineHeight: 1.12, color: c.fg }}>
        <Rise at={8}>What if every step</Rise>
        <br />
        <Rise at={40} style={gradientText}>
          could think?
        </Rise>
      </div>
    </AbsoluteFill>
  </SceneFade>
);

// ---------------------------------------------------------------- 3. logo
const Mark: React.FC<{ size: number; progress: number }> = ({ size, progress }) => {
  const d = (k: number) => interpolate(progress, [k, k + 0.35], [0, 1], clamp);
  return (
    <svg width={size} height={size} viewBox="0 0 100 100">
      <defs>
        <linearGradient id="mk" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0" stopColor={c.violet} />
          <stop offset="1" stopColor={c.teal} />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="96" height="96" rx="24" fill="url(#mk)" opacity={d(0)} />
      <path d="M33 48 L62 30 M33 52 L62 70" stroke="#fff" strokeWidth="5" strokeLinecap="round"
        strokeDasharray="40" strokeDashoffset={40 * (1 - d(0.35))} />
      <circle cx="30" cy="50" r={10 * d(0.2)} fill="#fff" />
      <circle cx="68" cy="28" r={10 * d(0.45)} fill="#fff" />
      <circle cx="68" cy="72" r={10 * d(0.6)} fill={c.bg} />
    </svg>
  );
};

const Logo: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sp = spring({ frame: f - 5, fps, config: { damping: 14, mass: 0.9 } });
  const mark = interpolate(f, [5, 70], [0, 1], clamp);
  const word = interpolate(f, [45, 85], [0, 1], { ...clamp, easing: ease });
  return (
    <SceneFade dur={S.logo.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
        <div style={{ display: "flex", alignItems: "center", gap: 40, transform: `scale(${0.85 + sp * 0.15})` }}>
          <Mark size={170} progress={mark} />
          <div
            style={{
              fontSize: 180,
              fontWeight: 800,
              letterSpacing: -7,
              color: c.fg,
              opacity: word,
              clipPath: `inset(0 ${(1 - word) * 100}% 0 0)`,
            }}
          >
            agentl
          </div>
        </div>
        <div style={{ marginTop: 40, fontSize: 46, color: c.muted, fontWeight: 500 }}>
          <Rise at={110}>Agentic ETL for Python.</Rise>
        </div>
      </AbsoluteFill>
    </SceneFade>
  );
};

// ---------------------------------------------------------------- 4. pipeline
const YAML: Array<[string, string?]> = [
  ["name: invoices", "k"],
  ["steps:", "k"],
  ["  - id: invoices"],
  ["    use: pdf.read"],
  ["  - id: clients"],
  ["    use: excel.read"],
  ["  - id: matched"],
  ["    use: join"],
  ["  - id: classify"],
  ["    agent: Classify each expense", "a"],
  ["    output: { category: str }", "a"],
  ["  - id: save"],
  ["    use: postgres.write"],
];

type NodeDef = { id: string; label: string; kind: "use" | "agent"; x: number; y: number; appear: number };
const NODES: NodeDef[] = [
  { id: "invoices", label: "invoices", kind: "use", x: 80, y: 120, appear: 120 },
  { id: "clients", label: "clients", kind: "use", x: 80, y: 420, appear: 210 },
  { id: "matched", label: "matched", kind: "use", x: 380, y: 270, appear: 300 },
  { id: "classify", label: "classify", kind: "agent", x: 680, y: 270, appear: 400 },
  { id: "save", label: "save", kind: "use", x: 680, y: 560, appear: 500 },
];
const EDGES: Array<[string, string]> = [
  ["invoices", "matched"],
  ["clients", "matched"],
  ["matched", "classify"],
  ["classify", "save"],
];
// Run phase: when each node turns running, then done.
const RUN_START = 640;
const RUN: Record<string, [number, number]> = {
  invoices: [RUN_START, RUN_START + 60],
  clients: [RUN_START + 20, RUN_START + 80],
  matched: [RUN_START + 90, RUN_START + 150],
  classify: [RUN_START + 160, RUN_START + 380],
  save: [RUN_START + 390, RUN_START + 440],
};
const NODE_W = 230;
const NODE_H = 96;

const PipelineNode: React.FC<{ n: NodeDef }> = ({ n }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - n.appear, fps, config: { damping: 15 } });
  const [run, done] = RUN[n.id];
  const state = f >= done ? "done" : f >= run ? "running" : "idle";
  const color = state === "done" ? c.green : state === "running" ? c.blue : n.kind === "agent" ? c.violet : c.line;
  const pulse = state === "running" ? 0.5 + 0.5 * Math.sin((f - run) / 5) : 0;
  return (
    <div
      style={{
        position: "absolute",
        left: n.x,
        top: n.y,
        width: NODE_W,
        height: NODE_H,
        transform: `scale(${s})`,
        opacity: s,
        borderRadius: n.kind === "agent" ? 48 : 18,
        background: c.panel,
        border: `3px solid ${color}`,
        boxShadow: `0 0 ${20 + pulse * 40}px ${color}${state === "idle" ? "00" : "88"}`,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: mono,
      }}
    >
      <div style={{ fontSize: 30, color: c.fg, fontWeight: 700 }}>{n.label}</div>
      <div style={{ fontSize: 20, color: n.kind === "agent" ? c.violet : c.muted }}>
        {n.kind === "agent" ? "LLM agent" : "connector"}
      </div>
      {state === "done" && (
        <div style={{ position: "absolute", right: -14, top: -14, width: 36, height: 36, borderRadius: 18,
          background: c.green, color: c.bg, fontSize: 24, fontWeight: 800, display: "flex",
          alignItems: "center", justifyContent: "center", fontFamily: sans }}>✓</div>
      )}
    </div>
  );
};

const Edges: React.FC = () => {
  const f = useCurrentFrame();
  const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));
  return (
    <svg width={960} height={720} style={{ position: "absolute", left: 0, top: 0, overflow: "visible" }}>
      {EDGES.map(([a, b]) => {
        const A = byId[a];
        const B = byId[b];
        const x1 = A.x + NODE_W;
        const y1 = A.y + NODE_H / 2;
        const vertical = B.y > A.y + NODE_H && Math.abs(B.x - A.x) < 10;
        const x2 = vertical ? B.x + NODE_W / 2 : B.x;
        const y2 = vertical ? B.y : B.y + NODE_H / 2;
        const sx = vertical ? A.x + NODE_W / 2 : x1;
        const sy = vertical ? A.y + NODE_H : y1;
        const d = vertical
          ? `M${sx},${sy} L${x2},${y2}`
          : `M${sx},${sy} C${sx + 70},${sy} ${x2 - 70},${y2} ${x2},${y2}`;
        const draw = interpolate(f, [B.appear - 10, B.appear + 25], [0, 1], clamp);
        const flowing = f >= RUN[a][1] - 10 && f < RUN[b][1];
        const lit = f >= RUN[b][0];
        return (
          <g key={a + b}>
            <path d={d} fill="none" stroke={lit ? c.green : c.line} strokeWidth={4}
              pathLength={1} strokeDasharray={1} strokeDashoffset={1 - draw} />
            {flowing && (
              <path d={d} fill="none" stroke={c.teal} strokeWidth={6} strokeLinecap="round"
                pathLength={100} strokeDasharray="6 14" strokeDashoffset={-f * 1.2} />
            )}
          </g>
        );
      })}
    </svg>
  );
};

const Pipeline: React.FC = () => {
  const f = useCurrentFrame();
  const chars = Math.floor(interpolate(f, [20, 520], [0, YAML.map((l) => l[0]).join("\n").length], clamp));
  let left = chars;
  const records = Math.round(interpolate(f, [RUN.classify[0], RUN.classify[1]], [0, 4008], clamp));
  const rejected = Math.round(interpolate(f, [RUN.classify[0] + 80, RUN.classify[1]], [0, 12], clamp));
  const caption = f < RUN_START - 20 ? "Declare it." : "Run it.";
  const capKey = f < RUN_START - 20 ? 0 : RUN_START;
  return (
    <SceneFade dur={S.pipeline.dur}>
      <AbsoluteFill style={{ fontFamily: sans }}>
        <div style={{ position: "absolute", top: 70, left: 0, right: 0, textAlign: "center", fontSize: 64, fontWeight: 800, color: c.fg, letterSpacing: -2 }}>
          <Rise at={capKey + 5} key={capKey}>{caption}</Rise>
        </div>
        {/* YAML editor */}
        <div style={{ position: "absolute", left: 110, top: 220, width: 640, height: 720, borderRadius: 22,
          background: "rgba(15,17,23,.92)", border: `1px solid ${c.line}`, boxShadow: "0 30px 80px rgba(0,0,0,.5)", overflow: "hidden" }}>
          <div style={{ display: "flex", gap: 10, padding: "18px 22px", borderBottom: `1px solid ${c.line}` }}>
            {["#ff5f56", "#ffbd2e", "#27c93f"].map((col) => <div key={col} style={{ width: 14, height: 14, borderRadius: 7, background: col }} />)}
            <div style={{ marginLeft: 16, color: c.muted, fontFamily: mono, fontSize: 20 }}>pipeline.yaml</div>
          </div>
          <div style={{ padding: "22px 28px", fontFamily: mono, fontSize: 27, lineHeight: 1.65 }}>
            {YAML.map(([text, tone], i) => {
              const shown = text.slice(0, Math.max(0, left));
              left -= text.length + 1;
              const col = tone === "a" ? c.violet : tone === "k" ? c.teal : c.fg;
              return (
                <div key={i} style={{ color: col, whiteSpace: "pre", minHeight: 44 }}>
                  {shown}
                  {left < 0 && left > -text.length - 2 && f < 560 && <span style={{ color: c.teal, opacity: (f % 40) < 20 ? 1 : 0 }}>▋</span>}
                </div>
              );
            })}
          </div>
        </div>
        {/* Graph */}
        <div style={{ position: "absolute", left: 860, top: 230, width: 960, height: 720 }}>
          <Edges />
          {NODES.map((n) => <PipelineNode key={n.id} n={n} />)}
          {f >= RUN.classify[0] && (
            <div style={{ position: "absolute", left: 380, top: 470, fontFamily: mono }}>
              <div style={{ fontSize: 64, fontWeight: 700, color: c.fg }}>{records.toLocaleString("en-US")}</div>
              <div style={{ fontSize: 22, color: c.muted }}>records classified</div>
              {rejected > 0 && (
                <div style={{ marginTop: 14, display: "inline-block", padding: "6px 14px", borderRadius: 10,
                  background: "rgba(251,191,36,.12)", border: `1px solid ${c.amber}`, color: c.amber, fontSize: 22 }}>
                  {rejected} rejected by schema
                </div>
              )}
            </div>
          )}
        </div>
      </AbsoluteFill>
    </SceneFade>
  );
};

// ---------------------------------------------------------------- 5. notification
const Notify: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - 20, fps, config: { damping: 16 } });
  const lines = [
    "Run invoices succeeded in 41s.",
    "12 of 4,020 invoices had no matching client;",
    "clients.xlsx looks outdated since Oct 1.",
  ];
  return (
    <SceneFade dur={S.notify.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
        <div style={{ fontSize: 64, fontWeight: 800, color: c.fg, letterSpacing: -2, marginBottom: 60 }}>
          <Rise at={5}>Know what happened.</Rise>{" "}
          <Rise at={30} style={gradientText}>And why.</Rise>
        </div>
        <div style={{ width: 1100, borderRadius: 24, background: c.panel, border: `1px solid ${c.line}`,
          padding: "34px 40px", transform: `translateY(${(1 - s) * 80}px)`, opacity: s,
          boxShadow: "0 40px 100px rgba(0,0,0,.55)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 18, marginBottom: 20 }}>
            <div style={{ width: 54, height: 54, borderRadius: 14, background: `linear-gradient(135deg, ${c.violet}, ${c.teal})` }} />
            <div>
              <div style={{ color: c.fg, fontSize: 28, fontWeight: 700 }}>agentl <span style={{ color: c.muted, fontWeight: 400 }}>· #data-alerts</span></div>
              <div style={{ color: c.green, fontFamily: mono, fontSize: 22 }}>run.succeeded · rejected=12</div>
            </div>
          </div>
          {lines.map((l, i) => {
            const vis = Math.floor(interpolate(f, [70 + i * 50, 115 + i * 50], [0, l.length], clamp));
            return (
              <div key={i} style={{ color: i === 0 ? c.fg : c.muted, fontSize: 32, lineHeight: 1.5 }}>
                {l.slice(0, vis)}
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    </SceneFade>
  );
};

// ---------------------------------------------------------------- 6. features
const FEATURES: Array<[string, string]> = [
  ["YAML pipelines", "One file instead of a folder of scripts"],
  ["Agents with schemas", "LLM output validated on every record"],
  ["Connectors included", "CSV, Excel, PDF, Word, HTTP, SQL"],
  ["Resumable runs", "Per-record cache. Pay once."],
  ["Live dashboard", "Every run, every step, every event"],
  ["Any LLM", "Plug in your provider of choice"],
];
const Features: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <SceneFade dur={S.features.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 500px)", gap: 34 }}>
          {FEATURES.map(([title, sub], i) => {
            const s = spring({ frame: f - 10 - i * 12, fps, config: { damping: 15 } });
            return (
              <div key={title} style={{ height: 230, borderRadius: 24, padding: 36, background: "rgba(15,17,23,.85)",
                border: `1px solid ${c.line}`, opacity: s, transform: `translateY(${(1 - s) * 60}px)` }}>
                <div style={{ width: 48, height: 6, borderRadius: 3, marginBottom: 28,
                  background: `linear-gradient(90deg, ${c.violet}, ${c.teal})` }} />
                <div style={{ fontSize: 40, fontWeight: 800, color: c.fg, letterSpacing: -1 }}>{title}</div>
                <div style={{ fontSize: 26, color: c.muted, marginTop: 12 }}>{sub}</div>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    </SceneFade>
  );
};

// ---------------------------------------------------------------- 7. CTA
const CTA: React.FC = () => {
  const f = useCurrentFrame();
  const cmd = "pip install agentl";
  const typed = cmd.slice(0, Math.floor(interpolate(f, [70, 130], [0, cmd.length], clamp)));
  return (
    <SceneFade dur={S.cta.dur + 18}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
        <div style={{ display: "flex", alignItems: "center", gap: 28 }}>
          <Mark size={110} progress={interpolate(f, [0, 50], [0, 1], clamp)} />
          <div style={{ fontSize: 120, fontWeight: 800, letterSpacing: -5, color: c.fg }}>
            <Rise at={15}>agentl</Rise>
          </div>
        </div>
        <div style={{ marginTop: 50, padding: "22px 40px", borderRadius: 18, background: c.panel,
          border: `1px solid ${c.line}`, fontFamily: mono, fontSize: 44, color: c.fg, minWidth: 620,
          opacity: interpolate(f, [50, 70], [0, 1], clamp) }}>
          <span style={{ color: c.teal }}>$ </span>{typed}
          <span style={{ color: c.teal, opacity: (f % 40) < 20 ? 1 : 0 }}>▋</span>
        </div>
        <div style={{ marginTop: 50, fontSize: 40, fontWeight: 500 }}>
          <Rise at={150} style={gradientText}>agentl.com.br</Rise>
        </div>
        <div style={{ marginTop: 14, fontSize: 28, color: c.muted }}>
          <Rise at={175}>Open source · MIT · github.com/arimayuji/agentl</Rise>
        </div>
      </AbsoluteFill>
    </SceneFade>
  );
};

// ---------------------------------------------------------------- composition
export const Launch: React.FC = () => (
  <AbsoluteFill>
    <Backdrop />
    <Sequence from={S.hook.from} durationInFrames={S.hook.dur}><Hook /></Sequence>
    <Sequence from={S.question.from} durationInFrames={S.question.dur}><Question /></Sequence>
    <Sequence from={S.logo.from} durationInFrames={S.logo.dur}><Logo /></Sequence>
    <Sequence from={S.pipeline.from} durationInFrames={S.pipeline.dur}><Pipeline /></Sequence>
    <Sequence from={S.notify.from} durationInFrames={S.notify.dur}><Notify /></Sequence>
    <Sequence from={S.features.from} durationInFrames={S.features.dur}><Features /></Sequence>
    <Sequence from={S.cta.from} durationInFrames={S.cta.dur}><CTA /></Sequence>
  </AbsoluteFill>
);
