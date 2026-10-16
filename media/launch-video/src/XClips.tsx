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

const DOTS = Array.from({ length: 26 }, (_, i) => ({
  x: (i * 37) % 100,
  y: (i * 53) % 100,
  r: 6 + ((i * 13) % 18),
  speed: 0.6 + ((i * 7) % 10) / 10,
  color: ["#9fd0f5", "#ffc2a8", "#a8e6c0", "#fff1a8"][i % 4],
}));

/** White with the brand's color mesh, now alive: the blobs wander and breathe, a soft light turns
 * slowly, and pastel dots drift up at different speeds (parallax). */
const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / CLIP_X_FPS;
  const blob = (x: number, y: number, size: number, col: string) =>
    `radial-gradient(${size}% ${size * 0.9}% at ${x}% ${y}%, ${col}dd, transparent 70%)`;
  const breathe = (p: number) => 42 + Math.sin(t * 0.9 + p) * 8;
  return (
    <AbsoluteFill style={{ background: k.white, overflow: "hidden" }}>
      <AbsoluteFill style={{
        backgroundImage: [
          blob(12 + Math.sin(t * 0.55) * 16, 12 + Math.cos(t * 0.4) * 10, breathe(0), "#9fd0f5"),
          blob(88 + Math.cos(t * 0.5) * 14, 10 + Math.sin(t * 0.6) * 12, breathe(1.3), "#ffc2a8"),
          blob(90 - Math.sin(t * 0.45) * 14, 90 + Math.cos(t * 0.5) * 8, breathe(2.1), "#a8e6c0"),
          blob(10 + Math.cos(t * 0.35) * 12, 88 - Math.sin(t * 0.55) * 10, breathe(3.4), "#fff1a8"),
        ].join(", "),
      }} />
      <AbsoluteFill style={{
        background: `conic-gradient(from ${t * 18}deg at 50% 50%, transparent 0deg, rgba(255,255,255,.55) 60deg, transparent 140deg, transparent 360deg)`,
        mixBlendMode: "soft-light",
      }} />
      {DOTS.map((d, i) => {
        const y = (((d.y - t * 4 * d.speed) % 110) + 110) % 110 - 5;
        const x = d.x + Math.sin(t * 0.8 + i) * 2;
        return (
          <div key={i} style={{ position: "absolute", left: `${x}%`, top: `${y}%`, width: d.r, height: d.r,
            borderRadius: "50%", background: d.color, opacity: 0.55, filter: "blur(1px)" }} />
        );
      })}
    </AbsoluteFill>
  );
};

/** Headline words that spring in one by one (blur, rise, tilt, scale), then keep floating gently;
 * the accent line carries a light sweep. */
const Kinetic: React.FC<{ lines: Array<{ text: string; color: string }>; size?: number }> = ({ lines, size = 72 }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  let n = 0;
  return (
    <div style={{ fontSize: size, fontWeight: 800, letterSpacing: -2, lineHeight: 1.06 }}>
      {lines.map((line, li) => (
        <div key={li} style={{ display: "flex", flexWrap: "wrap", columnGap: size * 0.26 }}>
          {line.text.split(" ").map((word, wi) => {
            const idx = n++;
            const s = spring({ frame: f - idx * 3, fps, config: { damping: 12, stiffness: 140, mass: 0.7 } });
            const float = Math.sin(f / 22 + idx * 0.9) * 3;
            const sweep = ((f * 1.6 - idx * 8) % 260) - 60;
            const accent = li > 0;
            return (
              <span key={wi} style={{
                display: "inline-block", color: line.color,
                opacity: Math.min(1, s * 1.4),
                transform: `translateY(${(1 - s) * 60 + float}px) rotate(${(1 - s) * 8}deg) scale(${0.82 + s * 0.18})`,
                filter: s < 0.98 ? `blur(${(1 - s) * 12}px)` : undefined,
                ...(accent ? {
                  backgroundImage: `linear-gradient(100deg, ${line.color} ${sweep - 30}%, #ffffff ${sweep}%, ${line.color} ${sweep + 30}%)`,
                  WebkitBackgroundClip: "text", backgroundClip: "text", color: "transparent",
                } : {}),
              }}>{word}</span>
            );
          })}
        </div>
      ))}
    </div>
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
  <Audio src={staticFile("sfx/music.mp3")} volume={(f: number) => 0.11 * interpolate(f, [0, 15, frames - 30, frames], [0, 1, 1, 0], clamp)} />
);

const Sfx: React.FC<{ at: number; src: string; volume?: number }> = ({ at, src, volume = 0.5 }) => (
  <Sequence from={at} durationInFrames={24} layout="none">
    <Audio src={staticFile(src)} volume={volume * 0.6} />
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
        <Kinetic size={76} lines={[{ text: "Rodou a IA de novo", color: k.ink }, { text: "nos mesmos dados?", color: k.sunset }]} />
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
        <Kinetic lines={[{ text: "1 milhão de linhas.", color: k.ink }, { text: "CSV → filtro → SQLite.", color: k.blue }]} />
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
        <Kinetic size={70} lines={[{ text: "Uma pasta de XML de NF-e.", color: k.ink }, { text: "Uma planilha de itens.", color: k.palm }]} />
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

// ---------------------------------------------------------------- 4. receipt photo (coming soon, #160)
export const RECEIPT_FRAMES = 390;
const RECEIPTS = [
  { store: "PADARIA PALMEIRA", cnpj: "12.345.678/0001-90", date: "14/10/2026", total: "38,70" },
  { store: "MERCADO SOL", cnpj: "98.765.432/0001-10", date: "15/10/2026", total: "214,35" },
  { store: "POSTO RIO AZUL", cnpj: "11.222.333/0001-81", date: "15/10/2026", total: "180,00" },
];

/** A paper receipt drawn in CSS: fictitious store and values. */
const Receipt: React.FC<{ r: (typeof RECEIPTS)[number]; scan: number; marks: number }> = ({ r, scan, marks }) => {
  const line = (t: string, bold = false, size = 22) => (
    <div style={{ fontFamily: mono, fontSize: size, fontWeight: bold ? 700 : 400, color: "#2b2b2b", whiteSpace: "pre" }}>{t}</div>
  );
  const box = (on: number) => ({
    outline: on > 0 ? `3px solid ${k.blue}` : "3px solid transparent",
    background: on > 0 ? `rgba(0,112,201,${0.12 * on})` : "transparent",
    borderRadius: 6,
  });
  return (
    <div style={{ position: "relative", width: 380, padding: "26px 24px 30px", background: "#fffdf6",
      boxShadow: "0 30px 60px rgba(11,27,43,.22)", clipPath:
        "polygon(0 0,100% 0,100% 97%,95% 100%,90% 97%,85% 100%,80% 97%,75% 100%,70% 97%,65% 100%,60% 97%,55% 100%,50% 97%,45% 100%,40% 97%,35% 100%,30% 97%,25% 100%,20% 97%,15% 100%,10% 97%,5% 100%,0 97%)" }}>
      <div style={box(marks >= 1 ? 1 : 0)}>{line(r.store, true, 24)}</div>
      <div style={box(marks >= 2 ? 1 : 0)}>{line(`CNPJ ${r.cnpj}`, false, 18)}</div>
      {line("------------------------------", false, 18)}
      {line("PAO FRANCES KG      12,40", false, 18)}
      {line("CAFE 500G           18,90", false, 18)}
      {line("LEITE INTEGRAL 1L    7,40", false, 18)}
      {line("------------------------------", false, 18)}
      <div style={box(marks >= 3 ? 1 : 0)}>{line(`TOTAL R$     ${r.total.padStart(9)}`, true, 24)}</div>
      <div style={box(marks >= 4 ? 1 : 0)}>{line(`${r.date} 08:41`, false, 18)}</div>
      {line("CUPOM FISCAL ELETRONICO", false, 14)}
      {scan > 0 && scan < 1 && (
        <div style={{ position: "absolute", left: 0, right: 0, top: `${scan * 100}%`, height: 6,
          background: k.sun, boxShadow: `0 0 30px 10px ${k.sun}aa` }} />
      )}
    </div>
  );
};

export const ReceiptClip: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const photoAt = 20;
  const scanAt = 60;
  const markAt = 100;
  const tableAt = 190;
  const enter = spring({ frame: f - photoAt, fps, config: { damping: 13, stiffness: 120 } });
  const flash = interpolate(f, [photoAt, photoAt + 3, photoAt + 14], [0, 1, 0], clamp);
  const toSide = lerp(f, tableAt - 20, tableAt + 10, 0, 1);
  return (
    <AbsoluteFill style={{ fontFamily: sans }}>
      <Backdrop />
      <Music frames={RECEIPT_FRAMES} />
      <Sfx at={photoAt} src="sfx/key2.wav" volume={0.6} />
      <Sfx at={scanAt} src="sfx/whoosh-in.wav" volume={0.3} />
      {[0, 1, 2, 3].map((i) => <Sfx key={i} at={markAt + i * 14} src="sfx/tick.wav" volume={0.3} />)}
      {RECEIPTS.map((_, i) => <Sfx key={`r${i}`} at={tableAt + 20 + i * 22} src={`sfx/ding${i}.wav`} volume={0.35} />)}
      <AbsoluteFill style={{ padding: "96px 56px 64px" }}>
        <Kinetic size={70} lines={[{ text: "Foto do cupom.", color: k.ink }, { text: "Linha na planilha.", color: k.blue }]} />
      </AbsoluteFill>
      <Pop at={6} style={{ position: "absolute", top: 34, left: 56, padding: "10px 20px", borderRadius: 999,
        background: k.sun, color: k.ink, fontWeight: 800, fontSize: 24 }}>EM BREVE</Pop>
      {/* the photo */}
      <div style={{ position: "absolute", left: 300 - toSide * 240, top: 290 + toSide * 40,
        transform: `scale(${(0.85 + enter * 0.35) * (1 - toSide * 0.45)}) rotate(${(1 - enter) * -12 - 3 + toSide * 3}deg)`,
        transformOrigin: "top left", opacity: Math.min(1, enter * 1.5) }}>
        <Receipt r={RECEIPTS[0]} scan={interpolate(f, [scanAt, scanAt + 36], [0, 1], clamp)}
          marks={Math.floor(interpolate(f, [markAt, markAt + 56], [0, 4], clamp))} />
      </div>
      <AbsoluteFill style={{ background: "#fff", opacity: flash, pointerEvents: "none" }} />
      {/* the agent step and fields */}
      <div style={{ position: "absolute", left: 56, right: 56, bottom: 150, opacity: 1 - toSide }}>
        <Pop at={markAt + 60} style={{ fontFamily: mono, fontSize: 26, background: k.white, border: `2px solid ${k.line}`,
          borderRadius: 20, padding: "18px 24px" }}>
          <span style={{ color: k.blue }}>agent:</span> Total, data, loja e CNPJ do cupom.<br />
          <span style={{ color: k.blue }}>output:</span> {"{ total: float, date: str, store: str, cnpj: str }"}
        </Pop>
      </div>
      {/* the spreadsheet */}
      <div style={{ position: "absolute", left: 380, right: 40, top: 330, opacity: lerp(f, tableAt, tableAt + 10, 0, 1) }}>
        <div style={{ background: k.white, border: `2px solid ${k.line}`, borderRadius: 20, overflow: "hidden",
          boxShadow: "0 20px 50px rgba(11,27,43,.12)" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 0.9fr", padding: "12px 16px",
            background: "#e6f1fa", fontSize: 20, fontWeight: 700, color: k.blue }}>
            {["loja", "data", "total (R$)"].map((h) => <div key={h}>{h}</div>)}
          </div>
          {RECEIPTS.map((r, i) => {
            const s = spring({ frame: f - (tableAt + 20 + i * 22), fps, config: { damping: 14, stiffness: 170 } });
            return (
              <div key={r.store} style={{ padding: "12px 16px", borderTop: `1px solid ${k.line}`,
                opacity: Math.min(1, s * 1.5), transform: `translateX(${(1 - s) * 40}px)` }}>
                <div style={{ display: "grid", gridTemplateColumns: "1.5fr 1fr 0.9fr", fontSize: 20, color: k.ink }}>
                  <div style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.store}</div>
                  <div style={{ fontFamily: mono }}>{r.date}</div>
                  <div style={{ fontFamily: mono, textAlign: "right" }}>{r.total}</div>
                </div>
                <div style={{ fontFamily: mono, fontSize: 15, color: k.muted }}>{r.cnpj}</div>
              </div>
            );
          })}
        </div>
        <Pop at={tableAt + 100} style={{ marginTop: 18, display: "flex", gap: 12, flexWrap: "wrap" }}>
          {["IA com visão ou OCR local", "não paga 2x a mesma foto"].map((t) => (
            <div key={t} style={{ padding: "8px 16px", borderRadius: 999, background: k.white, border: `1px solid ${k.line}`,
              fontSize: 20, color: k.ink }}>✓ {t}</div>
          ))}
        </Pop>
      </div>
      <Sign at={330} />
      <div style={{ position: "absolute", bottom: 20, left: 0, right: 0, textAlign: "center", fontSize: 16, color: k.muted }}>
        dados fictícios
      </div>
    </AbsoluteFill>
  );
};
