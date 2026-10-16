import React from "react";
import { AbsoluteFill, Audio, Easing, Img, Sequence, interpolate, random, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { mono, sans } from "./theme";

// One fixed terminal: commands are typed, output streams in line by line, old lines scroll
// away. The window flies in at the start and out at the end. Carioca palette (media/brand).
// The output is sample output, labelled on screen.
export const TERMINAL_FPS = 30;

const k = {
  white: "#ffffff",
  blue: "#0070c9",
  sun: "#ffd400",
  sunset: "#ff6b3d",
  palm: "#009c3b",
  ink: "#0b1b2b",
  muted: "#3d5266",
  line: "#d9e2ec",
};

type Tone = "cmd" | "out" | "ok" | "llm" | "cache" | "dim" | "gap";
type Line = { text: string; tone: Tone; at: number };

// Script: [text, tone, frames to wait before this line]. Commands are typed; output appears.
const SCRIPT: Array<[string, Tone, number]> = [
  ["agentl lint pipeline.yaml", "cmd", 24],
  ["0 errors, 0 warnings", "ok", 10],
  ["", "gap", 6],
  ["agentl plan pipeline.yaml", "cmd", 14],
  ["read      pdf.read       40 records", "out", 10],
  ["classify  agent          40 LLM calls", "llm", 5],
  ["save      excel.write", "out", 5],
  ["estimated cost  $0.08 - $0.14", "dim", 6],
  ["", "gap", 6],
  ["agentl run pipeline.yaml", "cmd", 14],
  ["read      40 records", "out", 10],
  ["classify  40/40  LLM  $0.12", "llm", 16],
  ["check     40 passed", "ok", 8],
  ["save      out/invoices.xlsx", "ok", 6],
  ["run ok in 21 s", "ok", 6],
  ["", "gap", 8],
  ["agentl run pipeline.yaml", "cmd", 14],
  ["read      40 records, unchanged", "out", 10],
  ["classify  40/40  cache  $0.00", "cache", 10],
  ["save      out/invoices.xlsx", "ok", 6],
  ["run ok in 0.4 s, nothing paid twice", "ok", 6],
];
const TYPE_SPEED = 1.4; // frames per character of a command

const LINES: Line[] = (() => {
  let t = 18;
  return SCRIPT.map(([text, tone, wait]) => {
    t += wait;
    const line = { text, tone, at: t };
    if (tone === "cmd") t += Math.ceil(text.length * TYPE_SPEED) + 6;
    return line;
  });
})();
const END = LINES[LINES.length - 1].at + 70;
const EXIT = 24;
export const TERMINAL_FRAMES = END + EXIT;

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const ease = Easing.bezier(0.16, 1, 0.3, 1);
const easeIn = Easing.bezier(0.7, 0, 0.84, 0);

const color: Record<Tone, string> = {
  cmd: k.ink,
  out: k.ink,
  ok: k.palm,
  llm: k.sunset,
  cache: k.blue,
  dim: k.muted,
  gap: k.ink,
};

const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / TERMINAL_FPS;
  const blob = (x: number, y: number, col: string) => `radial-gradient(40% 35% at ${x}% ${y}%, ${col}cc, transparent 70%)`;
  return (
    <AbsoluteFill style={{
      background: k.white,
      backgroundImage: [
        blob(4 + Math.sin(t * 0.4) * 6, 10, "#9fd0f5"),
        blob(38 + Math.cos(t * 0.3) * 8, -6, "#fff1a8"),
        blob(98, 4 + Math.sin(t * 0.35) * 6, "#ffc2a8"),
        blob(100 - Math.cos(t * 0.4) * 5, 96, "#a8e6c0"),
        blob(2, 100, "#9fd0f5"),
      ].join(", "),
    }} />
  );
};

const TermLine: React.FC<{ line: Line; row: number; rows: number; lineH: number; fontSize: number }> = ({
  line,
  row,
  rows,
  lineH,
  fontSize,
}) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = f - line.at;
  if (local < 0 || line.tone === "gap") return null;
  const isCmd = line.tone === "cmd";
  const chars = isCmd ? Math.min(line.text.length, Math.floor(local / TYPE_SPEED)) : line.text.length;
  const typing = isCmd && chars < line.text.length;
  // Entry: output lines slide in from the left, blurred; commands just appear and type.
  const s = spring({ frame: local, fps, config: { damping: 15, stiffness: 170 } });
  const enterX = isCmd ? 0 : (1 - s) * -60;
  const enterBlur = isCmd ? 0 : (1 - s) * 8;
  // Exit: lines pushed past the top fade and blur away.
  const out = interpolate(row, [-1, 0], [1, 0], clamp);
  const flash = line.tone === "cache" ? interpolate(local, [0, 6, 30], [0, 1, 0], clamp) : 0;
  return (
    <div style={{
      position: "absolute", left: 0, right: 0, top: row * lineH, height: lineH,
      display: "flex", alignItems: "center", whiteSpace: "pre", fontSize,
      color: color[line.tone], fontWeight: isCmd ? 700 : 400,
      opacity: Math.min(1, s * 1.5) * (1 - out) * (row >= rows ? 0 : 1),
      transform: `translateX(${enterX}px) translateY(${out * -20}px)`,
      filter: enterBlur + out * 6 > 0.3 ? `blur(${enterBlur + out * 6}px)` : undefined,
      background: flash ? `rgba(255,212,0,${0.35 * flash})` : undefined, borderRadius: 8,
    }}>
      {isCmd && <span style={{ color: k.blue }}>$ </span>}
      {line.text.slice(0, chars)}
      {(typing || (isCmd && local < line.text.length * TYPE_SPEED + 12)) && (
        <span style={{ background: k.ink, opacity: Math.floor(f / 7) % 2, marginLeft: 2 }}>&nbsp;</span>
      )}
    </div>
  );
};

// Sound: a keystroke per typed character (one of six variants, slightly random volume), Enter
// when a command is sent, a soft tick per output line, a whoosh in and out. Made by scripts/sfx.py.
type Sfx = { at: number; src: string; volume: number };
const SFX: Sfx[] = [
  { at: 0, src: "sfx/whoosh-in.wav", volume: 0.5 },
  ...LINES.flatMap((l, n): Sfx[] => {
    if (l.tone === "gap") return [];
    if (l.tone !== "cmd") return [{ at: l.at, src: "sfx/tick.wav", volume: 0.18 }];
    const keys = [...l.text].map((ch, i) => ({
      at: l.at + Math.ceil(i * TYPE_SPEED),
      src: `sfx/key${Math.floor(random(`k${n}-${i}`) * 6)}.wav`,
      volume: (ch === " " ? 0.35 : 0.55) + random(`v${n}-${i}`) * 0.2,
    }));
    return [...keys, { at: l.at + Math.ceil(l.text.length * TYPE_SPEED) + 4, src: "sfx/enter.wav", volume: 0.7 }];
  }),
  { at: END - 4, src: "sfx/whoosh-out.wav", volume: 0.45 },
];

const Sound: React.FC = () => (
  <>
    {SFX.map((x, i) => (
      <Sequence key={i} from={x.at} durationInFrames={20} layout="none">
        <Audio src={staticFile(x.src)} volume={x.volume} />
      </Sequence>
    ))}
  </>
);

export const Terminal: React.FC = () => {
  const f = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const portrait = height > width;
  const winW = portrait ? 960 : 1400;
  const rows = portrait ? 18 : 13;
  const fontSize = portrait ? 31 : 34;
  const lineH = fontSize * 1.7;

  // Window in: rises, unblurs and tilts flat. Window out: zooms toward the camera and fades.
  const inS = spring({ frame: f - 2, fps, config: { damping: 14, stiffness: 90 } });
  const outP = interpolate(f, [END, END + EXIT], [0, 1], { ...clamp, easing: easeIn });
  const scale = (0.86 + inS * 0.14) * (1 + outP * 0.35);
  const tilt = (1 - inS) * 18;
  const blur = (1 - inS) * 14 + outP * 18;

  // Scroll: each line past the last row pushes the text up by one row, eased over 10 frames.
  const offset = LINES.slice(rows).reduce(
    (sum, l) => sum + interpolate(f, [l.at, l.at + 10], [0, 1], { ...clamp, easing: ease }), 0);

  const label = interpolate(f, [30, 50, END - 10, END], [0, 1, 1, 0], clamp);

  return (
    <AbsoluteFill style={{ fontFamily: sans }}>
      <Backdrop />
      <Audio src={staticFile("sfx/music.mp3")} volume={(f: number) => 0.14 * interpolate(f, [0, 20, TERMINAL_FRAMES - 40, TERMINAL_FRAMES], [0, 1, 1, 0], clamp)} />
      <Sound />
      <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", perspective: 1600 }}>
        <div style={{
          width: winW, borderRadius: 26, background: k.white, border: `1px solid ${k.line}`,
          boxShadow: "0 40px 100px rgba(11,27,43,.16)", overflow: "hidden",
          opacity: Math.min(1, inS * 1.4) * (1 - outP),
          transform: `translateY(${(1 - inS) * 120}px) scale(${scale}) rotateX(${tilt}deg)`,
          filter: blur > 0.3 ? `blur(${blur}px)` : undefined,
        }}>
          <div style={{ display: "flex", gap: 12, padding: "20px 26px", borderBottom: `1px solid ${k.line}`, alignItems: "center" }}>
            {["#ff5f56", "#ffbd2e", "#27c93f"].map((col) => <div key={col} style={{ width: 16, height: 16, borderRadius: 8, background: col }} />)}
            <div style={{ marginLeft: 16, color: k.muted, fontFamily: mono, fontSize: 24 }}>~/invoices</div>
            <div style={{ flex: 1 }} />
            <Img src={staticFile("agentl-wordmark-light.svg")} style={{ height: 30 }} />
          </div>
          <div style={{ position: "relative", height: rows * lineH, margin: "26px 34px", fontFamily: mono, overflow: "hidden" }}>
            {LINES.map((line, i) => (
              <TermLine key={i} line={line} row={i - offset} rows={rows} lineH={lineH} fontSize={fontSize} />
            ))}
          </div>
        </div>
        <div style={{ position: "absolute", bottom: portrait ? 220 : 60, fontSize: 22, color: k.muted, opacity: label }}>
          sample output
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
