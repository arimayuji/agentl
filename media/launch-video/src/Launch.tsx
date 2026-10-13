import React from "react";
import {
  AbsoluteFill,
  Easing,
  Sequence,
  interpolate,
  random,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { c, gradientText, mono, sans } from "./theme";

// ---------------------------------------------------------------- timeline
// Shots overlap by OVERLAP frames so one zooms out while the next zooms in.
const OVERLAP = 14;
const S = {
  hook: { from: 0, dur: 170 },
  question: { from: 156, dur: 150 },
  logo: { from: 292, dur: 175 },
  pipeline: { from: 453, dur: 640 },
  notify: { from: 1079, dur: 250 },
  features: { from: 1315, dur: 250 },
  cta: { from: 1551, dur: 290 },
};
export const LAUNCH_FRAMES = S.cta.from + S.cta.dur;

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const easeIn = Easing.bezier(0.7, 0, 0.84, 0);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const lerp = (f: number, a: number, b: number, from: number, to: number, e = ease) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: e });

/** Global data speed: particles drift, then rush while the pipeline runs. */
const dataSpeed = (f: number) =>
  interpolate(f, [0, S.pipeline.from + 330, S.pipeline.from + 380, S.pipeline.from + 560, S.notify.from + 60],
    [1.2, 1.2, 6, 6, 1.2], clamp);

// ---------------------------------------------------------------- shot wrapper
/** Zoom-through transition: enters from slightly far and blurred, exits by flying past the camera. */
const Shot: React.FC<{ dur: number; children: React.ReactNode; enter?: boolean }> = ({ dur, children, enter = true }) => {
  const f = useCurrentFrame();
  const inP = enter ? lerp(f, 0, OVERLAP + 6, 0, 1) : 1;
  const outP = lerp(f, dur - OVERLAP, dur, 0, 1, easeIn);
  const scale = interpolate(inP, [0, 1], [0.82, 1]) * interpolate(outP, [0, 1], [1, 1.6]);
  const blur = (1 - inP) * 18 + outP * 22;
  const opacity = Math.min(inP, 1 - outP);
  return (
    <AbsoluteFill style={{ opacity, transform: `scale(${scale})`, filter: blur > 0.3 ? `blur(${blur}px)` : undefined }}>
      {children}
    </AbsoluteFill>
  );
};

/** Characters spring in one by one, from below and blurred. */
const Chars: React.FC<{ text: string; at: number; step?: number; style?: React.CSSProperties }> = ({
  text,
  at,
  step = 2,
  style,
}) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <span style={{ display: "inline-block", whiteSpace: "pre", ...style }}>
      {[...text].map((ch, i) => {
        const s = spring({ frame: f - at - i * step, fps, config: { damping: 12, stiffness: 170, mass: 0.6 } });
        return (
          <span
            key={i}
            style={{
              display: "inline-block",
              opacity: Math.min(1, s * 1.4),
              transform: `translateY(${(1 - s) * 70}px) rotate(${(1 - s) * 8}deg)`,
              filter: `blur(${Math.max(0, 1 - s) * 10}px)`,
            }}
          >
            {ch}
          </span>
        );
      })}
    </span>
  );
};

// ---------------------------------------------------------------- background
const PARTICLES = Array.from({ length: 140 }, (_, i) => ({
  y: random(`y${i}`) * 1080,
  x0: random(`x${i}`) * 2200,
  v: 0.6 + random(`v${i}`) * 1.6,
  r: 1 + random(`r${i}`) * 2.6,
  hue: random(`h${i}`) > 0.6 ? c.teal : random(`h2${i}`) > 0.5 ? c.violet : c.indigo,
}));

const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / 60;
  // Integrate the speed curve so particles accelerate smoothly instead of jumping.
  let dist = 0;
  for (let k = 0; k < f; k += 4) dist += dataSpeed(k) * 4;
  const g1x = 30 + Math.sin(t * 0.5) * 14;
  const g2x = 70 + Math.cos(t * 0.45) * 14;
  const rush = interpolate(dataSpeed(f), [1.2, 6], [0, 1]);
  return (
    <AbsoluteFill style={{ background: c.bg, overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(900px 650px at ${g1x}% 28%, rgba(99,102,241,.28), transparent 70%),
                       radial-gradient(850px 650px at ${g2x}% 78%, rgba(45,212,191,.18), transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage: `linear-gradient(${c.line}66 1px, transparent 1px), linear-gradient(90deg, ${c.line}66 1px, transparent 1px)`,
          backgroundSize: "80px 80px",
          backgroundPosition: `${-(dist * 0.5) % 80}px ${-(f * 0.4) % 80}px`,
          maskImage: "radial-gradient(ellipse at center, black 25%, transparent 72%)",
        }}
      />
      <svg width={1920} height={1080} style={{ position: "absolute", inset: 0 }}>
        {PARTICLES.map((p, i) => {
          const x = ((p.x0 + dist * p.v * 2) % 2200) - 140;
          const len = 4 + rush * 60 * p.v;
          return (
            <line key={i} x1={x - len} y1={p.y} x2={x} y2={p.y} stroke={p.hue} strokeWidth={p.r}
              strokeLinecap="round" opacity={0.25 + rush * 0.35} />
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};

/** Slow handheld-like drift applied to everything above the backdrop. */
const Camera: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const f = useCurrentFrame();
  const t = f / 60;
  const x = Math.sin(t * 0.7) * 10 + Math.sin(t * 1.9) * 3;
  const y = Math.cos(t * 0.6) * 8 + Math.cos(t * 2.3) * 2;
  const r = Math.sin(t * 0.4) * 0.35;
  return <AbsoluteFill style={{ transform: `translate(${x}px, ${y}px) rotate(${r}deg)` }}>{children}</AbsoluteFill>;
};

// ---------------------------------------------------------------- 1. hook
const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const left = spring({ frame: f - 4, fps, config: { damping: 14, stiffness: 120 } });
  const right = spring({ frame: f - 16, fps, config: { damping: 14, stiffness: 120 } });
  const strike = lerp(f, 78, 98, 0, 1);
  const fall = (i: number) => Math.max(0, f - 104 - i * 3);
  const push = interpolate(f, [0, 170], [1, 1.12]);
  return (
    <Shot dur={S.hook.dur} enter={false}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans, transform: `scale(${push})` }}>
        <div style={{ fontSize: 124, fontWeight: 800, color: c.fg, letterSpacing: -4, textAlign: "center", lineHeight: 1.08 }}>
          <span style={{ display: "inline-block", transform: `translateX(${(1 - left) * -900}px)`, filter: `blur(${(1 - left) * 20}px)` }}>
            Data pipelines
          </span>{" "}
          <span style={{ display: "inline-block", transform: `translateX(${(1 - right) * 900}px)`, filter: `blur(${(1 - right) * 20}px)` }}>
            are still
          </span>
          <br />
          <span style={{ position: "relative", display: "inline-block", color: c.muted }}>
            {[..."glue code."].map((ch, i) => {
              const d = fall(i);
              const s = spring({ frame: f - 40 - i * 2, fps, config: { damping: 12, mass: 0.6 } });
              return (
                <span key={i} style={{
                  display: "inline-block", whiteSpace: "pre",
                  opacity: Math.min(1, s * 1.5) * interpolate(d, [0, 40], [1, 0], clamp),
                  transform: `translateY(${(1 - s) * 60 + d * d * 0.5}px) rotate(${d * (i % 2 ? 4 : -5)}deg)`,
                }}>{ch}</span>
              );
            })}
            <span style={{ position: "absolute", left: -10, top: "52%", height: 12, borderRadius: 6, background: c.amber,
              width: `calc(${strike * 100}% + 20px)`, opacity: interpolate(f, [104, 130], [1, 0], clamp),
              boxShadow: `0 0 30px ${c.amber}` }} />
          </span>
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 2. question
const Question: React.FC = () => {
  const f = useCurrentFrame();
  const sweep = interpolate(f, [40, 150], [-30, 130]);
  const push = interpolate(f, [0, 150], [1.08, 0.98]);
  return (
    <Shot dur={S.question.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans, transform: `scale(${push})` }}>
        <div style={{ fontSize: 128, fontWeight: 800, letterSpacing: -4, textAlign: "center", lineHeight: 1.1, color: c.fg }}>
          <Chars text="What if every step" at={6} step={1.4} />
          <br />
          <span style={{
            ...gradientText,
            backgroundImage: `linear-gradient(100deg, ${c.violet} ${sweep - 40}%, #fff ${sweep}%, ${c.teal} ${sweep + 40}%)`,
            filter: `drop-shadow(0 0 ${20 + 15 * Math.sin(f / 8)}px rgba(99,102,241,.55))`,
          }}>
            <Chars text="could think?" at={34} step={2.2} />
          </span>
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 3. logo
const Mark: React.FC<{ size: number; progress: number }> = ({ size, progress }) => {
  const d = (k: number) => interpolate(progress, [k, k + 0.35], [0, 1], clamp);
  return (
    <svg width={size} height={size} viewBox="0 0 100 100" style={{ overflow: "visible" }}>
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
  const sp = spring({ frame: f - 6, fps, config: { damping: 11, stiffness: 120 } });
  const flash = interpolate(f, [8, 16, 60], [0, 1, 0], clamp);
  const ring = lerp(f, 8, 70, 0, 1);
  return (
    <Shot dur={S.logo.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans }}>
        <div style={{ position: "absolute", width: 1400, height: 1400, borderRadius: "50%",
          background: `radial-gradient(circle, rgba(167,139,250,${0.55 * flash}) 0%, rgba(45,212,191,${0.25 * flash}) 30%, transparent 60%)` }} />
        <div style={{ position: "absolute", width: 300 + ring * 1500, height: 300 + ring * 1500, borderRadius: "50%",
          border: `3px solid rgba(167,139,250,${0.6 * (1 - ring)})` }} />
        <div style={{ display: "flex", alignItems: "center", gap: 40,
          transform: `scale(${0.6 + sp * 0.4}) rotate(${(1 - sp) * -12}deg)` }}>
          <Mark size={180} progress={lerp(f, 6, 60, 0, 1)} />
          <div style={{ fontSize: 190, fontWeight: 800, letterSpacing: -7, color: c.fg }}>
            <Chars text="agentl" at={24} step={3} />
          </div>
        </div>
        <div style={{ marginTop: 34, fontSize: 48, color: c.muted, fontWeight: 500 }}>
          <Chars text="Agentic ETL for Python." at={70} step={0.8} />
        </div>
      </AbsoluteFill>
    </Shot>
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

type NodeDef = { id: string; kind: "use" | "agent"; x: number; y: number; appear: number };
const NODES: NodeDef[] = [
  { id: "invoices", kind: "use", x: 80, y: 120, appear: 70 },
  { id: "clients", kind: "use", x: 80, y: 420, appear: 120 },
  { id: "matched", kind: "use", x: 380, y: 270, appear: 170 },
  { id: "classify", kind: "agent", x: 680, y: 270, appear: 220 },
  { id: "save", kind: "use", x: 680, y: 560, appear: 270 },
];
const EDGES: Array<[string, string]> = [
  ["invoices", "matched"],
  ["clients", "matched"],
  ["matched", "classify"],
  ["classify", "save"],
];
const RUN_START = 330;
const RUN: Record<string, [number, number]> = {
  invoices: [RUN_START, RUN_START + 40],
  clients: [RUN_START + 10, RUN_START + 50],
  matched: [RUN_START + 60, RUN_START + 100],
  classify: [RUN_START + 110, RUN_START + 230],
  save: [RUN_START + 240, RUN_START + 270],
};
const NODE_W = 230;
const NODE_H = 96;
const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));

/** Edge geometry as a cubic bezier (or a straight vertical line). */
const edgeGeom = (a: string, b: string) => {
  const A = byId[a];
  const B = byId[b];
  const vertical = B.y > A.y + NODE_H && Math.abs(B.x - A.x) < 10;
  if (vertical) {
    const p0 = { x: A.x + NODE_W / 2, y: A.y + NODE_H };
    const p3 = { x: B.x + NODE_W / 2, y: B.y };
    return { d: `M${p0.x},${p0.y} L${p3.x},${p3.y}`, pts: [p0, p0, p3, p3] };
  }
  const p0 = { x: A.x + NODE_W, y: A.y + NODE_H / 2 };
  const p3 = { x: B.x, y: B.y + NODE_H / 2 };
  const p1 = { x: p0.x + 70, y: p0.y };
  const p2 = { x: p3.x - 70, y: p3.y };
  return { d: `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`, pts: [p0, p1, p2, p3] };
};
const bez = (pts: Array<{ x: number; y: number }>, t: number) => {
  const u = 1 - t;
  const [a, b, cc, d] = pts;
  return {
    x: u * u * u * a.x + 3 * u * u * t * b.x + 3 * u * t * t * cc.x + t * t * t * d.x,
    y: u * u * u * a.y + 3 * u * u * t * b.y + 3 * u * t * t * cc.y + t * t * t * d.y,
  };
};

const PipelineNode: React.FC<{ n: NodeDef }> = ({ n }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - n.appear, fps, config: { damping: 11, stiffness: 160 } });
  const [run, done] = RUN[n.id];
  const state = f >= done ? "done" : f >= run ? "running" : "idle";
  const color = state === "done" ? c.green : state === "running" ? c.blue : n.kind === "agent" ? c.violet : "#3a3f52";
  const pulse = state === "running" ? 0.5 + 0.5 * Math.sin((f - run) / 4) : 0;
  const pop = spring({ frame: f - done, fps, config: { damping: 9, stiffness: 220 } });
  const kick = state === "done" ? 1 + 0.08 * Math.sin(Math.min(1, (f - done) / 14) * Math.PI) : 1;
  return (
    <div style={{
      position: "absolute", left: n.x, top: n.y, width: NODE_W, height: NODE_H,
      transform: `scale(${s * kick}) translateY(${(1 - s) * 40}px)`, opacity: Math.min(1, s * 1.3),
      borderRadius: n.kind === "agent" ? 48 : 18, background: c.panel, border: `3px solid ${color}`,
      boxShadow: `0 0 ${20 + pulse * 60}px ${color}${state === "idle" ? "00" : "99"}`,
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", fontFamily: mono,
    }}>
      <div style={{ fontSize: 30, color: c.fg, fontWeight: 700 }}>{n.id}</div>
      <div style={{ fontSize: 20, color: n.kind === "agent" ? c.violet : c.muted }}>{n.kind === "agent" ? "LLM agent" : "connector"}</div>
      {state === "done" && (
        <div style={{ position: "absolute", right: -14, top: -14, width: 38, height: 38, borderRadius: 19,
          background: c.green, color: c.bg, fontSize: 24, fontWeight: 800, display: "flex", alignItems: "center",
          justifyContent: "center", fontFamily: sans, transform: `scale(${pop})` }}>✓</div>
      )}
    </div>
  );
};

const Edges: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <svg width={960} height={720} style={{ position: "absolute", left: 0, top: 0, overflow: "visible" }}>
      {EDGES.map(([a, b]) => {
        const { d, pts } = edgeGeom(a, b);
        const draw = lerp(f, byId[b].appear - 6, byId[b].appear + 22, 0, 1);
        const flowing = f >= RUN[a][0] + 10 && f < RUN[b][1];
        const lit = f >= RUN[b][0];
        return (
          <g key={a + b}>
            <path d={d} fill="none" stroke={lit ? c.green : "#3a3f52"} strokeWidth={4}
              pathLength={1} strokeDasharray={1} strokeDashoffset={1 - draw} />
            {flowing &&
              Array.from({ length: 6 }, (_, k) => {
                const t = (((f - RUN[a][0]) / 40 + k / 6) % 1 + 1) % 1;
                const p = bez(pts, t);
                const q = bez(pts, Math.max(0, t - 0.06));
                return (
                  <g key={k}>
                    <line x1={q.x} y1={q.y} x2={p.x} y2={p.y} stroke={c.teal} strokeWidth={6} strokeLinecap="round" opacity={0.6} />
                    <circle cx={p.x} cy={p.y} r={7} fill="#fff" style={{ filter: `drop-shadow(0 0 8px ${c.teal})` }} />
                  </g>
                );
              })}
          </g>
        );
      })}
    </svg>
  );
};

const Pipeline: React.FC = () => {
  const f = useCurrentFrame();
  const total = YAML.map((l) => l[0]).join("\n").length;
  const chars = Math.floor(lerp(f, 6, 300, 0, total, Easing.linear));
  let left = chars;
  const records = Math.round(lerp(f, RUN.classify[0], RUN.classify[1], 0, 4008, Easing.out(Easing.quad)));
  const rejected = Math.round(lerp(f, RUN.classify[0] + 50, RUN.classify[1], 0, 12, Easing.linear));
  // Camera: start close on the editor, pan to the graph, push into the agent while it runs, pull back.
  const camX = interpolate(f, [0, 110, 200, RUN.classify[0], RUN.classify[0] + 60, RUN.classify[1] + 20, 640],
    [420, 420, 0, 0, -380, -380, 0], { ...clamp, easing: Easing.inOut(Easing.cubic) });
  const camY = interpolate(f, [RUN.classify[0], RUN.classify[0] + 60, RUN.classify[1] + 20, 640], [0, 40, 40, 0],
    { ...clamp, easing: Easing.inOut(Easing.cubic) });
  const camS = interpolate(f, [0, 110, 200, RUN.classify[0], RUN.classify[0] + 60, RUN.classify[1] + 20, 640],
    [1.3, 1.3, 1, 1, 1.32, 1.32, 1], { ...clamp, easing: Easing.inOut(Easing.cubic) });
  const caption = f < RUN_START - 10 ? "Declare it." : "Run it.";
  const capAt = f < RUN_START - 10 ? 8 : RUN_START - 6;
  return (
    <Shot dur={S.pipeline.dur}>
      <AbsoluteFill style={{ fontFamily: sans }}>
        <AbsoluteFill style={{ transform: `translate(${camX}px, ${camY}px) scale(${camS})` }}>
          <div style={{ position: "absolute", left: 110, top: 220, width: 640, height: 720, borderRadius: 22,
            background: "rgba(15,17,23,.94)", border: `1px solid ${c.line}`, boxShadow: "0 30px 80px rgba(0,0,0,.55)", overflow: "hidden" }}>
            <div style={{ display: "flex", gap: 10, padding: "18px 22px", borderBottom: `1px solid ${c.line}` }}>
              {["#ff5f56", "#ffbd2e", "#27c93f"].map((col) => <div key={col} style={{ width: 14, height: 14, borderRadius: 7, background: col }} />)}
              <div style={{ marginLeft: 16, color: c.muted, fontFamily: mono, fontSize: 20 }}>pipeline.yaml</div>
            </div>
            <div style={{ padding: "22px 28px", fontFamily: mono, fontSize: 27, lineHeight: 1.65 }}>
              {YAML.map(([text, tone], i) => {
                const shown = text.slice(0, Math.max(0, left));
                const active = left >= 0 && left <= text.length;
                left -= text.length + 1;
                const col = tone === "a" ? c.violet : tone === "k" ? c.teal : c.fg;
                return (
                  <div key={i} style={{ color: col, whiteSpace: "pre", minHeight: 44,
                    background: active && f < 300 ? "rgba(99,102,241,.12)" : undefined, borderRadius: 6 }}>
                    {shown}
                    {active && f < 300 && <span style={{ color: c.teal }}>▋</span>}
                  </div>
                );
              })}
            </div>
          </div>
          <div style={{ position: "absolute", left: 860, top: 230, width: 960, height: 720 }}>
            <Edges />
            {NODES.map((n) => <PipelineNode key={n.id} n={n} />)}
            {f >= RUN.classify[0] && (
              <div style={{ position: "absolute", left: 380, top: 460, fontFamily: mono,
                transform: `scale(${spring({ frame: f - RUN.classify[0], fps: 60, config: { damping: 12 } })})`, transformOrigin: "left top" }}>
                <div style={{ fontSize: 72, fontWeight: 700, color: c.fg, fontVariantNumeric: "tabular-nums" }}>
                  {records.toLocaleString("en-US")}
                </div>
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
        <div style={{ position: "absolute", top: 64, left: 0, right: 0, textAlign: "center", fontSize: 72,
          fontWeight: 800, color: c.fg, letterSpacing: -2, textShadow: "0 4px 30px rgba(0,0,0,.8)" }}>
          <Chars key={caption} text={caption} at={capAt} step={1.5} />
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 5. notification
const Notify: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - 16, fps, config: { damping: 13, stiffness: 110 } });
  const badge = spring({ frame: f - 60, fps, config: { damping: 9, stiffness: 200 } });
  const lines = [
    "Run invoices succeeded in 41s.",
    "12 of 4,020 invoices had no matching client;",
    "clients.xlsx looks outdated since Oct 1.",
  ];
  const push = interpolate(f, [0, 250], [1, 1.07]);
  return (
    <Shot dur={S.notify.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans, transform: `scale(${push})` }}>
        <div style={{ fontSize: 72, fontWeight: 800, color: c.fg, letterSpacing: -2, marginBottom: 56 }}>
          <Chars text="Know what happened. " at={4} step={0.9} />
          <span style={gradientText}><Chars text="And why." at={26} step={2} /></span>
        </div>
        <div style={{ width: 1120, borderRadius: 26, background: c.panel, border: `1px solid ${c.line}`, padding: "34px 40px",
          transform: `translateX(${(1 - s) * 900}px) rotate(${(1 - s) * 8}deg)`, opacity: Math.min(1, s * 1.5),
          boxShadow: "0 40px 100px rgba(0,0,0,.6)", position: "relative" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 18, marginBottom: 20 }}>
            <div style={{ width: 56, height: 56, borderRadius: 14, background: `linear-gradient(135deg, ${c.violet}, ${c.teal})` }} />
            <div>
              <div style={{ color: c.fg, fontSize: 28, fontWeight: 700 }}>agentl <span style={{ color: c.muted, fontWeight: 400 }}>· #data-alerts · now</span></div>
              <div style={{ color: c.green, fontFamily: mono, fontSize: 22 }}>run.succeeded · rejected=12</div>
            </div>
          </div>
          {lines.map((l, i) => {
            const vis = Math.floor(lerp(f, 50 + i * 34, 82 + i * 34, 0, l.length, Easing.linear));
            return <div key={i} style={{ color: i === 0 ? c.fg : c.muted, fontSize: 32, lineHeight: 1.5, minHeight: 48 }}>{l.slice(0, vis)}</div>;
          })}
          <div style={{ position: "absolute", right: -22, top: -22, padding: "10px 18px", borderRadius: 14, background: c.violet,
            color: "#fff", fontWeight: 700, fontSize: 22, transform: `scale(${badge}) rotate(${(1 - badge) * 20}deg)`,
            boxShadow: `0 10px 30px ${c.violet}88` }}>AI summary</div>
        </div>
      </AbsoluteFill>
    </Shot>
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
  const tiltX = interpolate(f, [0, 250], [18, 4]);
  const tiltY = interpolate(f, [0, 250], [-14, 6]);
  return (
    <Shot dur={S.features.dur}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans, perspective: 1600 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 500px)", gap: 34,
          transform: `rotateX(${tiltX}deg) rotateY(${tiltY}deg)`, transformStyle: "preserve-3d" }}>
          {FEATURES.map(([title, sub], i) => {
            const s = spring({ frame: f - 6 - i * 7, fps, config: { damping: 13, stiffness: 140 } });
            const glow = Math.max(0, Math.sin((f - 60 - i * 12) / 10));
            return (
              <div key={title} style={{ height: 230, borderRadius: 24, padding: 36, background: "rgba(15,17,23,.9)",
                border: `1px solid ${glow > 0.6 ? c.indigo : c.line}`, opacity: Math.min(1, s * 1.4),
                transform: `translateZ(${(1 - s) * 900}px) translateY(${(1 - s) * 80}px)`,
                filter: `blur(${(1 - s) * 12}px)`, boxShadow: `0 0 ${glow * 50}px rgba(99,102,241,${glow * 0.35})` }}>
                <div style={{ width: 48 + glow * 40, height: 6, borderRadius: 3, marginBottom: 28,
                  background: `linear-gradient(90deg, ${c.violet}, ${c.teal})` }} />
                <div style={{ fontSize: 40, fontWeight: 800, color: c.fg, letterSpacing: -1 }}>{title}</div>
                <div style={{ fontSize: 26, color: c.muted, marginTop: 12 }}>{sub}</div>
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 7. CTA
const CTA: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const cmd = "pip install agentl";
  const typed = cmd.slice(0, Math.floor(lerp(f, 40, 84, 0, cmd.length, Easing.linear)));
  const enter = f >= 96;
  const ok = spring({ frame: f - 104, fps, config: { damping: 12 } });
  const under = lerp(f, 150, 190, 0, 1);
  const pull = interpolate(f, [0, 290], [1.12, 1]);
  return (
    <Shot dur={S.cta.dur + 40}>
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", fontFamily: sans, transform: `scale(${pull})` }}>
        <div style={{ display: "flex", alignItems: "center", gap: 28 }}>
          <Mark size={116} progress={lerp(f, 0, 40, 0, 1)} />
          <div style={{ fontSize: 124, fontWeight: 800, letterSpacing: -5, color: c.fg }}><Chars text="agentl" at={10} step={2.5} /></div>
        </div>
        <div style={{ marginTop: 48, padding: "24px 40px", borderRadius: 18, background: c.panel, border: `1px solid ${c.line}`,
          fontFamily: mono, fontSize: 42, color: c.fg, minWidth: 760, opacity: lerp(f, 28, 40, 0, 1),
          transform: `scale(${enter && f < 104 ? 0.98 : 1})` }}>
          <div><span style={{ color: c.teal }}>$ </span>{typed}{!enter && <span style={{ color: c.teal, opacity: f % 30 < 15 ? 1 : 0 }}>▋</span>}</div>
          {enter && (
            <div style={{ color: c.green, fontSize: 30, marginTop: 10, opacity: ok, transform: `translateY(${(1 - ok) * 20}px)` }}>
              Successfully installed agentl-0.1.0
            </div>
          )}
        </div>
        <div style={{ marginTop: 46, fontSize: 46, fontWeight: 600, position: "relative" }}>
          <span style={gradientText}><Chars text="agentl.com.br" at={140} step={1.6} /></span>
          <div style={{ position: "absolute", left: 0, bottom: -8, height: 4, width: `${under * 100}%`, borderRadius: 2,
            background: `linear-gradient(90deg, ${c.violet}, ${c.teal})` }} />
        </div>
        <div style={{ marginTop: 18, fontSize: 28, color: c.muted }}>
          <Chars text="Open source · MIT · github.com/arimayuji/agentl" at={170} step={0.5} />
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- composition
export const Launch: React.FC = () => (
  <AbsoluteFill>
    <Backdrop />
    <Camera>
      <Sequence from={S.hook.from} durationInFrames={S.hook.dur}><Hook /></Sequence>
      <Sequence from={S.question.from} durationInFrames={S.question.dur}><Question /></Sequence>
      <Sequence from={S.logo.from} durationInFrames={S.logo.dur}><Logo /></Sequence>
      <Sequence from={S.pipeline.from} durationInFrames={S.pipeline.dur}><Pipeline /></Sequence>
      <Sequence from={S.notify.from} durationInFrames={S.notify.dur}><Notify /></Sequence>
      <Sequence from={S.features.from} durationInFrames={S.features.dur}><Features /></Sequence>
      <Sequence from={S.cta.from} durationInFrames={S.cta.dur}><CTA /></Sequence>
    </Camera>
  </AbsoluteFill>
);
