import React from "react";
import { AbsoluteFill, Easing, Img, Sequence, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { brand, mono, sans } from "./theme";

// Square teaser for X (plays muted in the feed, so every beat is on screen as text).
export const TEASER_FPS = 30;
export const TEASER_SIZE = 1080;

const rio = {
  white: "#ffffff",
  paper: "#f5f8fb",
  blue: "#0070c9",
  sun: "#ffd400",
  sunset: "#ff6b3d",
  palm: "#009c3b",
  ink: "#0b1b2b",
  navy: "#08131f",
  muted: "#3d5266",
  soft: "#9fb3c6",
};

const S = {
  hook: { from: 0, dur: 105 }, // 3.5 s
  turn: { from: 105, dur: 90 }, // 3 s
  yaml: { from: 195, dur: 150 }, // 5 s
  run: { from: 345, dur: 210 }, // 7 s
  dash: { from: 555, dur: 105 }, // 3.5 s
  cta: { from: 660, dur: 135 }, // 4.5 s
};
export const TEASER_FRAMES = S.cta.from + S.cta.dur;

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const lerp = (f: number, a: number, b: number, from: number, to: number) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: ease });

const Fade: React.FC<{ dur: number; children: React.ReactNode }> = ({ dur, children }) => {
  const f = useCurrentFrame();
  const o = Math.min(lerp(f, 0, 10, 0, 1), lerp(f, dur - 10, dur, 1, 0));
  const y = lerp(f, 0, 14, 24, 0);
  return <AbsoluteFill style={{ opacity: o, transform: `translateY(${y}px)` }}>{children}</AbsoluteFill>;
};

const Pop: React.FC<{ at: number; children: React.ReactNode; style?: React.CSSProperties }> = ({ at, children, style }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - at, fps, config: { damping: 14, mass: 0.6 } });
  return <div style={{ opacity: Math.min(1, s * 1.4), transform: `translateY(${(1 - s) * 30}px) scale(${0.96 + s * 0.04})`, ...style }}>{children}</div>;
};

const Dot: React.FC<{ size: number }> = ({ size }) => (
  <span style={{ display: "inline-block", width: size, height: size, borderRadius: "50%", background: rio.sun, marginLeft: size * 0.25 }} />
);

const Wordmark: React.FC<{ size: number; color?: string }> = ({ size, color = rio.ink }) => (
  <span style={{ font: `700 ${size}px ${brand}`, letterSpacing: -size * 0.03, color, display: "inline-flex", alignItems: "baseline" }}>
    agentl
    <Dot size={size * 0.15} />
  </span>
);

const Sky: React.FC = () => (
  <AbsoluteFill style={{ background: rio.white }}>
    <AbsoluteFill
      style={{
        background: `radial-gradient(600px 500px at 0% 0%, #9fd0f5aa, transparent 70%),
          radial-gradient(500px 400px at 100% 0%, #ffc2a8aa, transparent 70%),
          radial-gradient(600px 500px at 100% 100%, #a8e6c0aa, transparent 70%),
          radial-gradient(500px 400px at 30% 110%, #fff1a8cc, transparent 70%)`,
      }}
    />
  </AbsoluteFill>
);

// ---------------------------------------------------------------- shots
const Hook: React.FC = () => {
  const f = useCurrentFrame();
  const count = Math.round(lerp(f, 30, 70, 0, 10000));
  return (
    <AbsoluteFill style={{ padding: 90, justifyContent: "center", font: `800 76px/1.08 ${sans}`, color: rio.ink }}>
      <Pop at={0}>Your LLM pipeline failed at row 9,000.</Pop>
      <Pop at={28} style={{ marginTop: 36, color: rio.muted, fontWeight: 700, fontSize: 60 }}>
        You re-run it and pay for
      </Pop>
      <Pop at={34} style={{ font: `800 170px/1.05 ${sans}`, letterSpacing: -4, color: rio.sunset, marginTop: 10 }}>
        {count.toLocaleString("en-US")}
      </Pop>
      <Pop at={40} style={{ color: rio.muted, fontWeight: 700, fontSize: 60 }}>model calls. Again.</Pop>
    </AbsoluteFill>
  );
};

const Turn: React.FC = () => (
  <AbsoluteFill style={{ padding: 90, justifyContent: "center", font: `800 84px/1.1 ${sans}`, color: rio.ink }}>
    <Pop at={0}>
      With <Wordmark size={96} />
    </Pop>
    <Pop at={14} style={{ marginTop: 30 }}>
      you pay for the <span style={{ background: rio.sun, padding: "0 14px", borderRadius: 12 }}>1,000</span> that didn't finish.
    </Pop>
  </AbsoluteFill>
);

const Terminal: React.FC<{ lines: { t: string; c?: string; bg?: string; at: number; cmd?: boolean }[]; title: string }> = ({ lines, title }) => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{ padding: 60, justifyContent: "center" }}>
      <div style={{ background: rio.navy, borderRadius: 28, padding: "34px 40px", boxShadow: "0 30px 80px #0b1b2b40" }}>
        <div style={{ display: "flex", gap: 10, marginBottom: 22, alignItems: "center" }}>
          {[0, 1, 2].map((i) => (
            <span key={i} style={{ width: 14, height: 14, borderRadius: "50%", background: "#1b2d42" }} />
          ))}
          <span style={{ marginLeft: 16, font: `500 22px ${sans}`, color: rio.soft }}>{title}</span>
        </div>
        {lines.map((l, i) => {
          const shown = l.cmd ? Math.max(0, Math.min(l.t.length, Math.floor((f - l.at) * 1.6))) : f >= l.at ? l.t.length : 0;
          if (f < l.at) return <div key={i} style={{ height: 44 }} />;
          return (
            <div
              key={i}
              style={{
                font: `${l.cmd ? 700 : 400} 30px/44px ${mono}`,
                color: l.c ?? rio.soft,
                background: l.bg,
                borderRadius: 8,
                padding: l.bg ? "0 10px" : undefined,
                whiteSpace: "pre",
              }}
            >
              {l.cmd ? <span style={{ color: "#4aa8ff" }}>$ </span> : null}
              {l.t.slice(0, shown)}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

const Yaml: React.FC = () => (
  <Terminal
    title="tickets.yaml"
    lines={[
      { t: "steps:", at: 4 },
      { t: "  - id: tickets", at: 10 },
      { t: "    use: csv.read", at: 14 },
      { t: "  - id: triage", at: 22 },
      { t: "    agent: Classify the ticket.", at: 28, c: "#4aa8ff" },
      { t: "    output: { category: str, urgent: bool }", at: 34, c: "#4aa8ff" },
      { t: "  - id: report", at: 44 },
      { t: "    use: excel.write", at: 48 },
    ]}
  />
);

const Run: React.FC = () => (
  <Terminal
    title="real output · stand-in model"
    lines={[
      { t: "agentl run tickets.yaml", at: 2, cmd: true, c: rio.white },
      { t: "step.succeeded [tickets] 40 records", at: 28 },
      { t: "step.succeeded [triage] llm_calls: 40", at: 40, c: rio.white, bg: "#0070c966" },
      { t: "run.succeeded 0.35 s", at: 52, c: "#2fc46a" },
      { t: "agentl run tickets.yaml   # again", at: 80, cmd: true, c: rio.white },
      { t: "step.succeeded [tickets] 40 records", at: 112 },
      { t: "step.succeeded [triage] llm_calls: 0", at: 124, c: rio.ink, bg: rio.sun },
      { t: "run.succeeded 0.02 s", at: 136, c: "#2fc46a" },
    ]}
  />
);

const Dash: React.FC = () => {
  const f = useCurrentFrame();
  const s = lerp(f, 0, 30, 1.08, 1);
  return (
    <AbsoluteFill style={{ padding: 60, justifyContent: "center", alignItems: "center" }}>
      <Pop at={0} style={{ font: `800 54px ${sans}`, color: rio.ink, marginBottom: 30, textAlign: "center" }}>
        Every run, live.
      </Pop>
      <div style={{ borderRadius: 24, overflow: "hidden", boxShadow: "0 30px 80px #0b1b2b33", transform: `scale(${s})` }}>
        <Img src={staticFile("dashboard-crop.png")} style={{ width: 940, display: "block" }} />
      </div>
    </AbsoluteFill>
  );
};

const Cta: React.FC = () => (
  <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", textAlign: "center" }}>
    <Pop at={0}>
      <Wordmark size={210} />
    </Pop>
    <Pop at={14} style={{ font: `600 46px ${sans}`, color: rio.muted, marginTop: 10 }}>
      Agentic ETL in YAML
    </Pop>
    <Pop at={26} style={{ display: "flex", gap: 14, marginTop: 40, flexWrap: "wrap", justifyContent: "center", maxWidth: 900 }}>
      {["per-record cache", "budgets", "data checks", "MCP for Claude Code"].map((t, i) => (
        <span key={t} style={{ font: `700 30px ${sans}`, color: rio.white, background: [rio.blue, rio.palm, rio.sunset, rio.ink][i], padding: "12px 22px", borderRadius: 999 }}>
          {t}
        </span>
      ))}
    </Pop>
    <Pop at={44} style={{ font: `700 44px ${mono}`, color: rio.ink, background: rio.white, border: `3px solid ${rio.ink}`, borderRadius: 18, padding: "18px 30px", marginTop: 50 }}>
      pip install agentl-etl
    </Pop>
    <Pop at={56} style={{ font: `500 30px ${sans}`, color: rio.muted, marginTop: 26 }}>
      open source · github.com/arimayuji/agentl
    </Pop>
  </AbsoluteFill>
);

export const XTeaser: React.FC = () => (
  <AbsoluteFill style={{ fontFamily: sans }}>
    <Sky />
    {(
      [
        [S.hook, Hook],
        [S.turn, Turn],
        [S.yaml, Yaml],
        [S.run, Run],
        [S.dash, Dash],
        [S.cta, Cta],
      ] as const
    ).map(([s, C], i) => (
      <Sequence key={i} from={s.from} durationInFrames={s.dur}>
        <Fade dur={i === 5 ? s.dur + 30 : s.dur}>
          <C />
        </Fade>
      </Sequence>
    ))}
  </AbsoluteFill>
);
