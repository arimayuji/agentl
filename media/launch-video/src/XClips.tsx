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

// Short square clips for X: they autoplay muted in the feed, so the text on screen tells the whole
// story and the first two seconds carry the hook. Carioca palette (media/brand). Numbers on screen
// are the ones measured by `python -m benchmarks --compare`, or sample output where labelled.
export const CLIP_X_FPS = 30;
export const CLIP_X_SIZE = 1080;

const k = {
  white: "#ffffff",
  blue: "#0070c9",
  sun: "#ffd400",
  sunset: "#ff6b3d",
  palm: "#009c3b",
  ink: "#0b1b2b",
  muted: "#3d5266",
  line: "#d9e2ec",
  red: "#d93a2b",
};
const br = (v: number) => v.toFixed(2).replace(".", ","); // 0,12 for a Brazilian feed
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const ease = Easing.bezier(0.16, 1, 0.3, 1);
const lerp = (f: number, a: number, b: number, from: number, to: number) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: ease });

const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / CLIP_X_FPS;
  const blob = (x: number, y: number, col: string) => `radial-gradient(45% 40% at ${x}% ${y}%, ${col}cc, transparent 70%)`;
  return (
    <AbsoluteFill style={{
      background: k.white,
      backgroundImage: [
        blob(6 + Math.sin(t * 0.5) * 6, 8, "#9fd0f5"),
        blob(96, 6 + Math.cos(t * 0.4) * 6, "#ffc2a8"),
        blob(100 - Math.sin(t * 0.4) * 5, 96, "#a8e6c0"),
        blob(4, 98, "#fff1a8"),
      ].join(", "),
    }} />
  );
};

const Pop: React.FC<{ at: number; children: React.ReactNode; style?: React.CSSProperties }> = ({ at, children, style }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - at, fps, config: { damping: 13, stiffness: 160 } });
  return (
    <div style={{ opacity: Math.min(1, s * 1.5), transform: `translateY(${(1 - s) * 36}px) scale(${0.92 + s * 0.08})`, ...style }}>
      {children}
    </div>
  );
};

/** Wordmark and handle in the last second: the clip is shared without the post around it. */
const Sign: React.FC<{ at: number }> = ({ at }) => (
  <Pop at={at} style={{ position: "absolute", bottom: 56, width: "100%", display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
    <Img src={staticFile("agentl-wordmark-light.svg")} style={{ height: 64 }} />
    <div style={{ fontFamily: mono, fontSize: 26, color: k.muted }}>pip install agentl-etl</div>
  </Pop>
);

const Music: React.FC<{ frames: number }> = ({ frames }) => (
  <Audio src={staticFile("sfx/music.mp3")} volume={(f: number) => 0.22 * interpolate(f, [0, 15, frames - 30, frames], [0, 1, 1, 0], clamp)} />
);

const Sfx: React.FC<{ at: number; src: string; volume?: number }> = ({ at, src, volume = 0.5 }) => (
  <Sequence from={at} durationInFrames={24} layout="none">
    <Audio src={staticFile(src)} volume={volume} />
  </Sequence>
);

// ---------------------------------------------------------------- 1. "Pagou duas vezes?"
export const PAY_TWICE_FRAMES = 330;

/** A counter that rolls from `from` to `to` between frames a and b. */
const Money: React.FC<{ from: number; to: number; a: number; b: number; color: string; size: number }> = ({
  from, to, a, b, color, size,
}) => {
  const f = useCurrentFrame();
  const v = interpolate(f, [a, b], [from, to], { ...clamp, easing: Easing.out(Easing.cubic) });
  return <span style={{ fontFamily: mono, fontWeight: 700, fontSize: size, color }}>US$ {br(v)}</span>;
};

const Lane: React.FC<{ title: string; tone: string; second: number; secondNote: string; at: number }> = ({
  title, tone, second, secondNote, at,
}) => {
  const f = useCurrentFrame();
  const run1 = at + 20;
  const run2 = at + 120;
  const total = 0.12 + second;
  return (
    <div style={{ flex: 1, background: k.white, border: `2px solid ${k.line}`, borderRadius: 28, padding: "30px 30px 34px",
      boxShadow: "0 20px 50px rgba(11,27,43,.10)", display: "flex", flexDirection: "column", gap: 22 }}>
      <div style={{ fontFamily: sans, fontSize: 36, fontWeight: 800, color: tone }}>{title}</div>
      {[
        { label: "1ª execução", at: run1, to: 0.12, note: "500 chamadas" },
        { label: "2ª execução", at: run2, to: second, note: secondNote },
      ].map((r) => (
        <div key={r.label} style={{ opacity: lerp(f, r.at - 6, r.at + 4, 0, 1) }}>
          <div style={{ fontFamily: sans, fontSize: 24, color: k.muted }}>{r.label}</div>
          <Money from={0} to={r.to} a={r.at} b={r.at + 40} color={k.ink} size={60} />
          <div style={{ fontFamily: sans, fontSize: 22, color: r.to === 0 ? k.palm : k.muted, fontWeight: r.to === 0 ? 700 : 400 }}>
            {r.note}
          </div>
        </div>
      ))}
      <div style={{ borderTop: `2px solid ${k.line}`, paddingTop: 18, opacity: lerp(f, run2 + 46, run2 + 56, 0, 1) }}>
        <div style={{ fontFamily: sans, fontSize: 24, color: k.muted }}>total</div>
        <Money from={0} to={total} a={run2 + 46} b={run2 + 70} color={tone} size={76} />
      </div>
    </div>
  );
};

export const PayTwice: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{ fontFamily: sans }}>
      <Backdrop />
      <Music frames={PAY_TWICE_FRAMES} />
      <Sfx at={30} src="sfx/enter.wav" />
      <Sfx at={130} src="sfx/enter.wav" />
      <Sfx at={190} src="sfx/ding1.wav" volume={0.4} />
      <AbsoluteFill style={{ padding: "70px 60px", gap: 34 }}>
        <Pop at={0} style={{ fontSize: 76, fontWeight: 800, letterSpacing: -2, color: k.ink, lineHeight: 1.05 }}>
          Rodou a IA de novo<br /><span style={{ color: k.sunset }}>nos mesmos dados?</span>
        </Pop>
        <div style={{ display: "flex", gap: 28, opacity: lerp(f, 8, 20, 0, 1) }}>
          <Lane title="script comum" tone={k.red} second={0.12} secondNote="500 chamadas de novo" at={10} />
          <Lane title="agentl" tone={k.palm} second={0} secondNote="0 chamadas: cache" at={10} />
        </div>
      </AbsoluteFill>
      <Sign at={270} />
      <div style={{ position: "absolute", top: 30, right: 40, fontSize: 18, color: k.muted }}>exemplo: 500 registros</div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 2. "1 milhão de linhas"
export const MILLION_FRAMES = 360;
const RACE = [
  { name: "Python à mão", seconds: 3.03, mb: 0, tone: k.muted },
  { name: "agentl", seconds: 3.81, mb: 60, tone: k.blue },
  { name: "pandas", seconds: 3.9, mb: 157, tone: k.sunset },
];

export const Million: React.FC = () => {
  const f = useCurrentFrame();
  const start = 60;
  const maxS = 4.2;
  const clock = Math.max(0, (f - start) / CLIP_X_FPS) * 1.4; // the race plays 1.4x faster than real time
  const memAt = 230;
  return (
    <AbsoluteFill style={{ fontFamily: sans }}>
      <Backdrop />
      <Music frames={MILLION_FRAMES} />
      <Sfx at={start} src="sfx/whoosh-in.wav" volume={0.35} />
      {RACE.map((r, i) => (
        <Sfx key={r.name} at={start + Math.round((r.seconds / 1.4) * CLIP_X_FPS)} src={`sfx/ding${i}.wav`} volume={0.35} />
      ))}
      <AbsoluteFill style={{ padding: "70px 60px", gap: 26 }}>
        <Pop at={0} style={{ fontSize: 72, fontWeight: 800, letterSpacing: -2, color: k.ink, lineHeight: 1.05 }}>
          1 milhão de linhas.<br /><span style={{ color: k.blue }}>CSV → filtro → SQLite.</span>
        </Pop>
        <Pop at={14} style={{ fontSize: 26, color: k.muted }}>mesma saída nas três, processo limpo, 4 CPUs</Pop>
        <div style={{ display: "flex", flexDirection: "column", gap: 30, marginTop: 18 }}>
          {RACE.map((r) => {
            const done = clock >= r.seconds;
            const w = Math.min(clock, r.seconds) / maxS;
            const memW = lerp(f, memAt, memAt + 30, 0, r.mb / 170);
            return (
              <div key={r.name}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 32, fontWeight: 700, color: r.tone }}>
                  <span>{r.name}</span>
                  <span style={{ fontFamily: mono, opacity: f >= start ? 1 : 0 }}>
                    {br(done ? r.seconds : Math.min(clock, r.seconds))} s {done ? "✓" : ""}
                  </span>
                </div>
                <div style={{ height: 34, borderRadius: 17, background: k.line, marginTop: 8, overflow: "hidden" }}>
                  <div style={{ width: `${w * 100}%`, height: "100%", borderRadius: 17, background: r.tone }} />
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 10, opacity: lerp(f, memAt - 6, memAt + 4, 0, 1) }}>
                  <div style={{ width: `${memW * 70}%`, minWidth: 6, height: 14, borderRadius: 7, background: `${r.tone}88` }} />
                  <span style={{ fontFamily: mono, fontSize: 24, color: k.muted }}>{r.mb ? `${r.mb} MB` : "≈0 MB"}</span>
                </div>
              </div>
            );
          })}
        </div>
        <Pop at={memAt + 30} style={{ fontSize: 30, fontWeight: 700, color: k.ink }}>
          Empata com o pandas usando 40% da memória.
        </Pop>
      </AbsoluteFill>
      <Sign at={300} />
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 3. NF-e (coming soon, #161)
export const NFE_FRAMES = 390;
const NFE_FILES = Array.from({ length: 14 }, (_, i) => `35261012345678000190550010000${String(4310 + i * 37).padStart(5, "0")}.xml`);
const NFE_ROWS = [
  ["Distribuidora Sol", "8471.30.12", "5102", "Notebook 14\"", "3.899,00"],
  ["Distribuidora Sol", "8528.52.20", "5102", "Monitor 24\"", "1.249,90"],
  ["Papelaria Rio", "4802.56.10", "5405", "Papel A4 (cx)", "289,50"],
  ["Café Palmeira", "0901.21.00", "5102", "Café 1 kg", "64,90"],
  ["Café Palmeira", "1701.99.00", "5102", "Açúcar 5 kg", "27,80"],
  ["TecnoSul", "8517.62.59", "6102", "Roteador Wi-Fi", "459,00"],
];

export const Nfe: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const flyStart = 40;
  const tableAt = 175;
  const yamlAt = 120;
  return (
    <AbsoluteFill style={{ fontFamily: sans }}>
      <Backdrop />
      <Music frames={NFE_FRAMES} />
      <Sfx at={flyStart} src="sfx/whoosh-in.wav" volume={0.35} />
      {NFE_ROWS.map((_, i) => (
        <Sfx key={i} at={tableAt + 10 + i * 12} src="sfx/tick.wav" volume={0.25} />
      ))}
      <Sfx at={tableAt + 95} src="sfx/ding2.wav" volume={0.4} />
      <AbsoluteFill style={{ padding: "96px 56px 64px" }}>
        <Pop at={0} style={{ fontSize: 70, fontWeight: 800, letterSpacing: -2, color: k.ink, lineHeight: 1.05 }}>
          Uma pasta de XML de NF-e.<br /><span style={{ color: k.palm }}>Uma planilha de itens.</span>
        </Pop>
      </AbsoluteFill>
      {/* XML files fly into the YAML card */}
      {NFE_FILES.map((name, i) => {
        const at = flyStart + i * 5;
        const p = interpolate(f, [at, at + 40], [0, 1], { ...clamp, easing: Easing.in(Easing.cubic) });
        if (f < at || p >= 1) return null;
        const x0 = 80 + (i % 4) * 230;
        const y0 = 380 + Math.floor(i / 4) * 80;
        return (
          <div key={name} style={{ position: "absolute", left: x0 + (430 - x0) * p, top: y0 + (560 - y0) * p,
            transform: `scale(${1 - p * 0.7}) rotate(${(i % 2 ? 6 : -6) * (1 - p)}deg)`, opacity: 1 - p * 0.6,
            padding: "10px 14px", borderRadius: 12, background: k.white, border: `2px solid ${k.palm}`,
            fontFamily: mono, fontSize: 16, color: k.ink, boxShadow: "0 8px 20px rgba(11,27,43,.12)", whiteSpace: "nowrap" }}>
            &lt;NFe&gt; {name.slice(0, 18)}…
          </div>
        );
      })}
      {/* the pipeline */}
      <div style={{ position: "absolute", left: 56, right: 56, top: 360,
        opacity: lerp(f, yamlAt, yamlAt + 12, 0, 1) * (f < tableAt ? 1 : lerp(f, tableAt, tableAt + 12, 1, 0)),
        transform: `translateY(${lerp(f, yamlAt, yamlAt + 14, 30, 0)}px)` }}>
        <div style={{ background: k.white, border: `2px solid ${k.line}`, borderRadius: 24, padding: "26px 30px",
          fontFamily: mono, fontSize: 30, lineHeight: 1.6, whiteSpace: "pre", boxShadow: "0 20px 50px rgba(11,27,43,.10)" }}>
          <div><span style={{ color: k.blue }}>- use:</span> nfe.read</div>
          <div>{"  "}<span style={{ color: k.blue }}>with:</span> {"{ path: \"xml/*.xml\", level: item }"}</div>
          <div><span style={{ color: k.blue }}>- use:</span> excel.write</div>
        </div>
      </div>
      {/* the spreadsheet */}
      <div style={{ position: "absolute", left: 40, right: 40, top: 330, opacity: lerp(f, tableAt, tableAt + 10, 0, 1) }}>
        <div style={{ background: k.white, border: `2px solid ${k.line}`, borderRadius: 22, overflow: "hidden",
          boxShadow: "0 20px 50px rgba(11,27,43,.12)" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1.1fr 0.6fr 1.4fr 0.9fr", gap: 0,
            background: "#e8f5ec", padding: "14px 18px", fontSize: 22, fontWeight: 700, color: k.palm }}>
            {["emitente", "NCM", "CFOP", "item", "valor (R$)"].map((h) => <div key={h}>{h}</div>)}
          </div>
          {NFE_ROWS.map((row, i) => {
            const s = spring({ frame: f - (tableAt + 10 + i * 12), fps, config: { damping: 14, stiffness: 170 } });
            return (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "1.5fr 1.1fr 0.6fr 1.4fr 0.9fr",
                padding: "14px 18px", fontSize: 22, color: k.ink, borderTop: `1px solid ${k.line}`,
                opacity: Math.min(1, s * 1.5), transform: `translateX(${(1 - s) * 40}px)` }}>
                {row.map((cell, j) => (
                  <div key={j} style={{ fontFamily: j === 1 || j === 2 || j === 4 ? mono : sans,
                    textAlign: j === 4 ? "right" : "left", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{cell}</div>
                ))}
              </div>
            );
          })}
        </div>
        <Pop at={tableAt + 95} style={{ marginTop: 22, display: "flex", gap: 14, flexWrap: "wrap" }}>
          {["chave validada", "impostos por item", "nota cancelada marcada", "sem SEFAZ, sem LLM"].map((t) => (
            <div key={t} style={{ padding: "10px 18px", borderRadius: 999, background: k.white, border: `1px solid ${k.line}`,
              fontSize: 22, color: k.ink }}>✓ {t}</div>
          ))}
        </Pop>
      </div>
      <Pop at={6} style={{ position: "absolute", top: 34, left: 56, padding: "10px 20px", borderRadius: 999,
        background: k.sun, color: k.ink, fontWeight: 800, fontSize: 24 }}>EM BREVE</Pop>
      <Sign at={330} />
      <div style={{ position: "absolute", bottom: 20, left: 0, right: 0, textAlign: "center", fontSize: 16, color: k.muted }}>
        dados fictícios
      </div>
    </AbsoluteFill>
  );
};
