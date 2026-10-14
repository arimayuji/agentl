import React from "react";
import { AbsoluteFill, Easing, Sequence, interpolate, random, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { brand, mono, sans } from "./theme";

// Square launch teaser for X. It plays muted in the feed: every beat is on screen, and it moves.
export const TEASER_FPS = 60;
export const TEASER_SIZE = 1080;

const rio = {
  white: "#ffffff",
  blue: "#0070c9",
  sky: "#9fd0f5",
  sun: "#ffd400",
  sunset: "#ff6b3d",
  palm: "#009c3b",
  ink: "#0b1b2b",
  navy: "#08131f",
  muted: "#3d5266",
};

const OVERLAP = 14;
const S = {
  hook: { from: 0, dur: 170 },
  rerun: { from: 156, dur: 170 },
  turn: { from: 312, dur: 150 },
  pipe: { from: 448, dur: 400 },
  feats: { from: 834, dur: 150 },
  cta: { from: 970, dur: 260 },
};
export const TEASER_FRAMES = S.cta.from + S.cta.dur;

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const easeIn = Easing.bezier(0.7, 0, 0.84, 0);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const lerp = (f: number, a: number, b: number, from: number, to: number, e = ease) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: e });

/** How fast the data particles fly, over the whole video. */
const speed = (f: number) =>
  interpolate(f, [0, 200, 230, 320, 560, 600, 700, 760, 1230], [1, 1, 7, 2, 2, 7, 7, 1.5, 1.5], clamp);

// ---------------------------------------------------------------- building blocks
/** Zoom-through transition: in from far and blurred, out by flying past the camera. */
const Shot: React.FC<{ dur: number; children: React.ReactNode; enter?: boolean; exit?: boolean }> = ({
  dur,
  children,
  enter = true,
  exit = true,
}) => {
  const f = useCurrentFrame();
  const inP = enter ? lerp(f, 0, OVERLAP + 6, 0, 1) : 1;
  const outP = exit ? lerp(f, dur - OVERLAP, dur, 0, 1, easeIn) : 0;
  const scale = interpolate(inP, [0, 1], [0.8, 1]) * interpolate(outP, [0, 1], [1, 1.7]);
  const blur = (1 - inP) * 18 + outP * 24;
  return (
    <AbsoluteFill
      style={{
        opacity: Math.min(inP, 1 - outP),
        transform: `scale(${scale})`,
        filter: blur > 0.3 ? `blur(${blur}px)` : undefined,
      }}
    >
      {children}
    </AbsoluteFill>
  );
};

/** Letters spring in one by one, from below, rotated and blurred. */
const Chars: React.FC<{ text: string; at: number; step?: number; style?: React.CSSProperties }> = ({
  text,
  at,
  step = 1.6,
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
              transform: `translateY(${(1 - s) * 80}px) rotate(${(1 - s) * 10}deg)`,
              filter: `blur(${Math.max(0, 1 - s) * 12}px)`,
            }}
          >
            {ch}
          </span>
        );
      })}
    </span>
  );
};

const Slam: React.FC<{ at: number; children: React.ReactNode; style?: React.CSSProperties }> = ({ at, children, style }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - at, fps, config: { damping: 9, stiffness: 220, mass: 0.7 } });
  return (
    <div style={{ display: "inline-block", opacity: Math.min(1, s * 2), transform: `scale(${2.6 - s * 1.6})`, filter: `blur(${(1 - Math.min(1, s)) * 14}px)`, ...style }}>
      {children}
    </div>
  );
};

/** Screen shake that decays after `at`. */
const shake = (f: number, at: number, amp = 18) => {
  const d = f - at;
  if (d < 0 || d > 24) return "translate(0,0)";
  const k = amp * Math.exp(-d / 6);
  return `translate(${Math.sin(d * 3.1) * k}px, ${Math.cos(d * 2.3) * k}px)`;
};

const SunDot: React.FC<{ size: number; at: number; drop?: boolean }> = ({ size, at, drop }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - at, fps, config: { damping: drop ? 7 : 10, stiffness: 160, mass: 0.8 } });
  const y = drop ? (1 - s) * -420 : 0;
  return (
    <span
      style={{
        display: "inline-block",
        width: size,
        height: size,
        borderRadius: "50%",
        background: rio.sun,
        marginLeft: size * 0.25,
        transform: `translateY(${y}px) scale(${drop ? 1 : s})`,
        boxShadow: `0 0 ${size * 0.8}px ${rio.sun}aa`,
      }}
    />
  );
};

// ---------------------------------------------------------------- background
const PARTICLES = Array.from({ length: 110 }, (_, i) => ({
  y: random(`y${i}`) * TEASER_SIZE,
  x0: random(`x${i}`) * 1400,
  v: 0.6 + random(`v${i}`) * 1.6,
  r: 1.5 + random(`r${i}`) * 3,
  col: [rio.blue, rio.palm, rio.sun, rio.sunset, rio.sky][Math.floor(random(`c${i}`) * 5)],
}));

const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / 60;
  let dist = 0;
  for (let k = 0; k < f; k += 4) dist += speed(k) * 4;
  const rush = interpolate(speed(f), [1, 7], [0, 1], clamp);
  return (
    <AbsoluteFill style={{ background: rio.white, overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(520px 480px at ${8 + Math.sin(t * 0.6) * 10}% ${6 + Math.cos(t * 0.5) * 8}%, ${rio.sky}cc, transparent 70%),
            radial-gradient(460px 420px at ${96 + Math.cos(t * 0.5) * 8}% ${4 + Math.sin(t * 0.7) * 8}%, #ffc2a8bb, transparent 70%),
            radial-gradient(520px 480px at ${98 + Math.sin(t * 0.4) * 8}% ${98 + Math.cos(t * 0.6) * 6}%, #a8e6c0bb, transparent 70%),
            radial-gradient(480px 420px at ${20 + Math.cos(t * 0.5) * 10}% ${104 + Math.sin(t * 0.5) * 6}%, #fff1a8dd, transparent 70%)`,
        }}
      />
      <svg width={TEASER_SIZE} height={TEASER_SIZE} style={{ position: "absolute", inset: 0 }}>
        {PARTICLES.map((p, i) => {
          const x = ((p.x0 + dist * p.v * 2) % 1400) - 160;
          const len = 6 + rush * 90 * p.v;
          return (
            <line key={i} x1={x - len} y1={p.y} x2={x} y2={p.y} stroke={p.col} strokeWidth={p.r} strokeLinecap="round"
              opacity={0.18 + rush * 0.4} />
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};

const Camera: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const f = useCurrentFrame();
  const t = f / 60;
  const x = Math.sin(t * 0.7) * 8 + Math.sin(t * 1.9) * 2;
  const y = Math.cos(t * 0.6) * 6 + Math.cos(t * 2.3) * 2;
  const r = Math.sin(t * 0.4) * 0.3;
  return <AbsoluteFill style={{ transform: `translate(${x}px, ${y}px) rotate(${r}deg)` }}>{children}</AbsoluteFill>;
};

const center = { alignItems: "center", justifyContent: "center", textAlign: "center" } as const;

// ---------------------------------------------------------------- 1. hook: the pipeline fails
const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const left = spring({ frame: f - 4, fps, config: { damping: 14, stiffness: 120 } });
  const progress = lerp(f, 30, 95, 0, 0.9, Easing.linear);
  const broke = f >= 96;
  const push = interpolate(f, [0, 170], [1, 1.1]);
  return (
    <Shot dur={S.hook.dur} enter={false}>
      <AbsoluteFill style={{ ...center, fontFamily: sans, transform: `scale(${push}) ${shake(f, 96, 22)}` }}>
        <div style={{ fontSize: 96, fontWeight: 800, color: rio.ink, letterSpacing: -3, lineHeight: 1.05 }}>
          <span style={{ display: "inline-block", transform: `translateX(${(1 - left) * -900}px)`, filter: `blur(${(1 - left) * 20}px)` }}>
            Your LLM pipeline
          </span>
          <br />
          {broke ? (
            <Slam at={96} style={{ color: rio.sunset, fontSize: 150 }}>
              failed.
            </Slam>
          ) : (
            <span style={{ color: rio.muted, fontSize: 64 }}>is running…</span>
          )}
        </div>
        <div style={{ width: 760, marginTop: 50 }}>
          <div style={{ height: 22, borderRadius: 11, background: "#e2e9f0", overflow: "hidden" }}>
            <div style={{ width: `${progress * 100}%`, height: "100%", borderRadius: 11, background: broke ? rio.sunset : rio.blue }} />
          </div>
          <div style={{ font: `700 34px ${mono}`, color: broke ? rio.sunset : rio.muted, marginTop: 16 }}>
            row {Math.round(progress * 10000).toLocaleString("en-US")} of 10,000
          </div>
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 2. the re-run bill
const Rerun: React.FC = () => {
  const f = useCurrentFrame();
  const n = Math.round(lerp(f, 30, 110, 0, 10000, Easing.bezier(0.5, 0, 0.2, 1)));
  const hot = interpolate(f, [30, 110], [0, 1], clamp);
  return (
    <Shot dur={S.rerun.dur}>
      <AbsoluteFill style={{ ...center, fontFamily: sans, transform: shake(f, 112, 14) }}>
        <div style={{ fontSize: 70, fontWeight: 800, color: rio.ink, letterSpacing: -2 }}>
          <Chars text="Re-run it. Pay for" at={4} />
        </div>
        <div
          style={{
            font: `800 230px/1 ${sans}`,
            letterSpacing: -8,
            color: rio.sunset,
            marginTop: 10,
            textShadow: `0 0 ${hot * 60}px ${rio.sunset}66`,
            transform: `scale(${1 + hot * 0.06})`,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {n.toLocaleString("en-US")}
        </div>
        <div style={{ fontSize: 70, fontWeight: 800, color: rio.ink, letterSpacing: -2 }}>
          <Chars text="calls. Again." at={70} />
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 3. the turn
const Rays: React.FC<{ at: number; size: number }> = ({ at, size }) => {
  const f = useCurrentFrame();
  const o = lerp(f, at, at + 20, 0, 0.55);
  const rot = (f - at) * 0.4;
  return (
    <svg width={size} height={size} style={{ position: "absolute", opacity: o, transform: `rotate(${rot}deg)` }} viewBox="-100 -100 200 200">
      {Array.from({ length: 16 }, (_, i) => (
        <polygon key={i} points="0,0 -6,-100 6,-100" fill={rio.sun} opacity={0.35} transform={`rotate(${(i * 360) / 16})`} />
      ))}
    </svg>
  );
};

const Turn: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <Shot dur={S.turn.dur}>
      <AbsoluteFill style={{ ...center, fontFamily: sans }}>
        <Rays at={30} size={1400} />
        <div style={{ font: `700 220px ${brand}`, letterSpacing: -7, color: rio.ink, display: "flex", alignItems: "baseline", position: "relative" }}>
          <Chars text="agentl" at={4} step={3} />
          <SunDot size={34} at={30} />
        </div>
        <div style={{ fontSize: 60, fontWeight: 800, color: rio.ink, letterSpacing: -1.5, marginTop: 20, position: "relative" }}>
          <Chars text="pays only for what" at={50} step={1} />
          <br />
          <span style={{ display: "inline-block", background: rio.sun, padding: "0 16px", borderRadius: 14, transform: `scale(${lerp(f, 80, 100, 0.6, 1)})`, opacity: lerp(f, 80, 92, 0, 1) }}>
            didn't finish.
          </span>
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 4. the pipeline runs, twice
const NODES = [
  { x: 190, label: "tickets", sub: "csv.read", col: rio.blue },
  { x: 540, label: "triage", sub: "LLM agent", col: rio.palm },
  { x: 890, label: "report", sub: "excel.write", col: rio.sunset },
];

const Pipe: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const run1 = 90; // records flow, calls count up
  const run2 = 250; // again: records zip through the cache
  const calls1 = Math.round(lerp(f, run1, run1 + 80, 0, 40, Easing.linear));
  const second = f >= run2;
  const calls = second ? 0 : calls1;
  const flash = lerp(f, run2 + 30, run2 + 40, 0, 1) * lerp(f, run2 + 40, run2 + 80, 1, 0);
  const label = second ? "run again: same data" : f >= run1 ? "agentl run tickets.yaml" : "";
  const flow = (start: number, dur: number, n: number) =>
    Array.from({ length: n }, (_, i) => {
      const p = (f - start - i * (dur / n)) / 40;
      return p > 0 && p < 1 ? p : null;
    });
  const dots1 = flow(run1, 80, 14);
  const dots2 = flow(run2, 24, 14).map((p) => (p === null ? null : Math.min(1, p * 2.5)));
  return (
    <Shot dur={S.pipe.dur}>
      <AbsoluteFill style={{ fontFamily: sans }}>
        <div style={{ position: "absolute", top: 120, width: "100%", textAlign: "center", font: `700 40px ${mono}`, color: rio.muted, height: 50 }}>
          {label ? <Chars key={label} text={label} at={second ? run2 : run1} step={0.6} /> : null}
        </div>
        <svg width={TEASER_SIZE} height={TEASER_SIZE} style={{ position: "absolute", inset: 0 }}>
          {[0, 1].map((i) => {
            const draw = lerp(f, 20 + i * 14, 50 + i * 14, 0, 1);
            const x1 = NODES[i].x + 120;
            const x2 = NODES[i + 1].x - 120;
            return <line key={i} x1={x1} y1={470} x2={x1 + (x2 - x1) * draw} y2={470} stroke="#c9d6e3" strokeWidth={8} strokeLinecap="round" />;
          })}
          {[...dots1.map((p) => [p, rio.blue] as const), ...dots2.map((p) => [p, rio.sun] as const)].map(([p, col], i) => {
            if (p === null) return null;
            const x = NODES[0].x + (NODES[2].x - NODES[0].x) * p;
            return <circle key={i} cx={x} cy={470} r={13} fill={col} stroke={rio.white} strokeWidth={3} />;
          })}
        </svg>
        {NODES.map((n, i) => {
          const s = spring({ frame: f - 6 - i * 8, fps, config: { damping: 12, stiffness: 150 } });
          const lit = (f > run1 + 10 + i * 25 && f < run1 + 120) || (f > run2 + i * 8 && f < run2 + 60);
          return (
            <div
              key={n.label}
              style={{
                position: "absolute",
                left: n.x - 120,
                top: 400,
                width: 240,
                height: 140,
                borderRadius: i === 1 ? 70 : 26,
                background: rio.white,
                border: `5px solid ${lit ? n.col : "#c9d6e3"}`,
                boxShadow: lit ? `0 0 40px ${n.col}88` : "0 12px 30px #0b1b2b1a",
                transform: `scale(${s})`,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <div style={{ font: `800 40px ${sans}`, color: rio.ink }}>{n.label}</div>
              <div style={{ font: `500 24px ${mono}`, color: rio.muted }}>{n.sub}</div>
            </div>
          );
        })}
        <div style={{ position: "absolute", top: 640, width: "100%", textAlign: "center" }}>
          <div style={{ font: `600 44px ${sans}`, color: rio.muted }}>model calls</div>
          <div
            style={{
              display: "inline-block",
              font: `800 200px/1.05 ${sans}`,
              letterSpacing: -6,
              fontVariantNumeric: "tabular-nums",
              color: second ? rio.ink : rio.blue,
              background: second ? rio.sun : "transparent",
              borderRadius: 30,
              padding: "0 40px",
              transform: `scale(${1 + flash * 0.25})`,
              boxShadow: second ? `0 0 ${80 * flash + 20}px ${rio.sun}` : undefined,
            }}
          >
            {calls}
          </div>
        </div>
        <AbsoluteFill style={{ background: rio.sun, opacity: flash * 0.35, pointerEvents: "none" }} />
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 5. what else
const FEATS = [
  { t: "per-record cache", col: rio.blue, from: [-700, -200] },
  { t: "budgets", col: rio.palm, from: [700, -260] },
  { t: "data checks", col: rio.sunset, from: [-700, 260] },
  { t: "MCP for Claude Code", col: rio.ink, from: [700, 220] },
];

const Feats: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  return (
    <Shot dur={S.feats.dur}>
      <AbsoluteFill style={{ ...center, fontFamily: sans, gap: 28 }}>
        {FEATS.map((x, i) => {
          const s = spring({ frame: f - 8 - i * 9, fps, config: { damping: 13, stiffness: 140 } });
          return (
            <div
              key={x.t}
              style={{
                font: `800 64px ${sans}`,
                color: rio.white,
                background: x.col,
                padding: "18px 40px",
                borderRadius: 999,
                transform: `translate(${(1 - s) * x.from[0]}px, ${(1 - s) * x.from[1]}px) rotate(${(1 - s) * (i % 2 ? 14 : -14)}deg)`,
                opacity: Math.min(1, s * 1.5),
                boxShadow: `0 16px 40px ${x.col}55`,
              }}
            >
              {x.t}
            </div>
          );
        })}
      </AbsoluteFill>
    </Shot>
  );
};

// ---------------------------------------------------------------- 6. call to action
const Cta: React.FC = () => {
  const f = useCurrentFrame();
  const cmd = "pip install agentl-etl";
  const typed = cmd.slice(0, Math.max(0, Math.floor((f - 60) / 2.2)));
  const cursor = Math.floor(f / 18) % 2 === 0;
  return (
    <Shot dur={S.cta.dur} exit={false}>
      <AbsoluteFill style={{ ...center, fontFamily: sans }}>
        <Rays at={40} size={1500} />
        <div style={{ font: `700 230px ${brand}`, letterSpacing: -7, color: rio.ink, display: "flex", alignItems: "baseline", position: "relative" }}>
          <Chars text="agentl" at={4} step={2.5} />
          <SunDot size={36} at={34} drop />
        </div>
        <div style={{ font: `700 54px ${sans}`, color: rio.muted, marginTop: 4, position: "relative" }}>
          <Chars text="Agentic ETL in YAML" at={40} step={1} />
        </div>
        <div
          style={{
            position: "relative",
            font: `700 52px ${mono}`,
            color: rio.white,
            background: rio.navy,
            borderRadius: 20,
            padding: "22px 36px",
            marginTop: 60,
            minWidth: 760,
            textAlign: "left",
            opacity: lerp(f, 50, 62, 0, 1),
            transform: `translateY(${lerp(f, 50, 66, 40, 0)}px)`,
          }}
        >
          <span style={{ color: "#4aa8ff" }}>$ </span>
          {typed}
          <span style={{ opacity: cursor ? 1 : 0, color: rio.sun }}>▍</span>
        </div>
        <div style={{ position: "relative", font: `600 34px ${sans}`, color: rio.muted, marginTop: 34, opacity: lerp(f, 120, 140, 0, 1) }}>
          open source · github.com/arimayuji/agentl
        </div>
      </AbsoluteFill>
    </Shot>
  );
};

export const XTeaser: React.FC = () => (
  <AbsoluteFill>
    <Backdrop />
    <Camera>
      {(
        [
          [S.hook, Hook],
          [S.rerun, Rerun],
          [S.turn, Turn],
          [S.pipe, Pipe],
          [S.feats, Feats],
          [S.cta, Cta],
        ] as const
      ).map(([s, C], i) => (
        <Sequence key={i} from={s.from} durationInFrames={s.dur}>
          <C />
        </Sequence>
      ))}
    </Camera>
  </AbsoluteFill>
);
