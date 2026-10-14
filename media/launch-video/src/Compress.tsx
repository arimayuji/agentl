import React from "react";
import { AbsoluteFill, Easing, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { LEGACY, PIPELINE } from "./compress/sources";
import { brand, c, gradientText, mono, sans } from "./theme";

// ------------------------------------------------------------------ timeline (60 fps)
const T = {
  scroll: [30, 380], // read the legacy file
  zoom: [380, 520], // zoom out to the whole file
  bands: [500, 760], // colour its responsibilities
  collapse: [760, 1150], // bands fly into the YAML
  run: [1150, 1470], // the pipeline runs
  end: [1450, 1740],
};
export const COMPRESS_FRAMES = T.end[1];

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const inOut = Easing.bezier(0.65, 0, 0.35, 1);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const lerp = (f: number, a: number, b: number, from: number, to: number, e = ease) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: e });

// ------------------------------------------------------------------ what the legacy file spends its lines on
type Section = { label: string; from: number; to: number; color: string; yaml?: [number, number]; builtin?: string };
const SECTIONS: Section[] = [
  { label: "Setup, config, logging", from: 1, to: 41, color: c.indigo, yaml: [1, 2] },
  { label: "State file + retries", from: 42, to: 74, color: "#f472b6", builtin: "retries · backoff · per-record cache" },
  { label: "Extract: paginated API, CSV, S3", from: 75, to: 117, color: c.blue, yaml: [4, 21] },
  { label: "Transform: pandas", from: 118, to: 140, color: c.teal, yaml: [23, 26] },
  { label: "AI: prompt, JSON parsing, retries", from: 141, to: 187, color: c.violet, yaml: [27, 33] },
  { label: "Load: SQL upsert in batches", from: 188, to: 211, color: c.green, yaml: [35, 39] },
  { label: "Slack notifications", from: 212, to: 221, color: c.amber, yaml: [41, 45] },
  { label: "Orchestration, error handling", from: 222, to: 253, color: "#fb7185", builtin: "run history · events · dashboard" },
];

// ------------------------------------------------------------------ syntax colouring
const PY = /(#.*$)|("""|f?"[^"]*"|f?'[^']*')|\b(def|return|for|in|if|not|import|from|as|with|try|except|raise|while|break|else|or|and|None|True|False|continue|elif|lambda)\b|(\b\d+(?:\.\d+)?\b)/g;
const YML = /(#.*$)|(\$\{[^}]+\})|("[^"]*")|(^\s*-?\s*[\w]+(?=:))|(\b(?:true|false|\d+(?:\.\d+)?)\b)/g;

const Code: React.FC<{ line: string; kind: "py" | "yaml" }> = ({ line, kind }) => {
  const re = kind === "py" ? PY : YML;
  const out: React.ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of line.matchAll(re)) {
    if (m.index! > last) out.push(line.slice(last, m.index));
    const color =
      kind === "py"
        ? m[1] ? c.muted : m[2] ? c.green : m[3] ? c.violet : c.amber
        : m[1] ? c.muted : m[2] ? c.amber : m[3] ? c.green : m[4] ? c.blue : c.violet;
    out.push(<span key={i++} style={{ color }}>{m[0]}</span>);
    last = m.index! + m[0].length;
  }
  out.push(line.slice(last));
  return <>{out}</>;
};

// ------------------------------------------------------------------ backdrop
const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const a = f / 240;
  return (
    <AbsoluteFill style={{ background: c.bg }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(900px 700px at ${30 + Math.sin(a) * 8}% ${35 + Math.cos(a) * 6}%, ${c.indigo}22, transparent 70%),
                       radial-gradient(800px 600px at ${75 + Math.cos(a * 0.8) * 8}% ${70 + Math.sin(a) * 6}%, ${c.teal}1a, transparent 70%)`,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage: `linear-gradient(${c.line}55 1px, transparent 1px), linear-gradient(90deg, ${c.line}55 1px, transparent 1px)`,
          backgroundSize: "64px 64px",
          maskImage: "radial-gradient(ellipse at center, black 30%, transparent 75%)",
          opacity: 0.5,
        }}
      />
    </AbsoluteFill>
  );
};

const Window: React.FC<{
  title: string;
  x: number;
  y: number;
  w: number;
  h: number;
  children: React.ReactNode;
  style?: React.CSSProperties;
}> = ({ title, x, y, w, h, children, style }) => (
  <div
    style={{
      position: "absolute",
      left: x,
      top: y,
      width: w,
      height: h,
      background: `${c.panel}f2`,
      border: `1px solid ${c.line}`,
      borderRadius: 18,
      overflow: "hidden",
      boxShadow: "0 40px 120px #000a",
      ...style,
    }}
  >
    <div style={{ height: 46, display: "flex", alignItems: "center", gap: 8, padding: "0 18px", borderBottom: `1px solid ${c.line}` }}>
      {["#ff5f57", "#febc2e", "#28c840"].map((col) => (
        <div key={col} style={{ width: 13, height: 13, borderRadius: 7, background: col }} />
      ))}
      <div style={{ marginLeft: 14, fontFamily: mono, fontSize: 19, color: c.muted }}>{title}</div>
    </div>
    <div style={{ position: "absolute", top: 46, left: 0, right: 0, bottom: 0, overflow: "hidden" }}>{children}</div>
  </div>
);

// ------------------------------------------------------------------ scene 1-4: the legacy file
const LH = 34;
const CODE_FONT = 23;
const WIN = { x: 210, y: 150, w: 1500, h: 860 };
const VIEW_H = WIN.h - 46;
const FIT = (VIEW_H - 40) / (LEGACY.length * LH);

const Legacy: React.FC = () => {
  const f = useCurrentFrame();
  const maxScroll = LEGACY.length * LH - VIEW_H + 40;
  const scroll = lerp(f, T.scroll[0], T.scroll[1], 0, maxScroll, inOut) * (1 - lerp(f, T.zoom[0], T.zoom[1], 0, 1, inOut));
  const scale = lerp(f, T.zoom[0], T.zoom[1], 1, FIT, inOut);
  const codeX = lerp(f, T.zoom[0], T.zoom[1], 0, 130, inOut);
  // collapse: window slides left and fades as the YAML takes over
  const out = lerp(f, T.collapse[0] + 40, T.collapse[0] + 200, 0, 1, inOut);
  const dim = lerp(f, T.collapse[0], T.collapse[0] + 120, 1, 0.12);
  const exit = lerp(f, T.run[0] - 20, T.run[0] + 40, 0, 1, inOut);
  const lines = Math.round(lerp(f, T.scroll[0], T.scroll[1] - 40, 24, LEGACY.length, Easing.linear));
  return (
    <div style={{ position: "absolute", inset: 0, opacity: (1 - exit) * (1 - 0.9 * out), transform: `translateX(${-out * 640}px) scale(${1 - out * 0.35})`, transformOrigin: "0% 50%" }}>
      <Window title={`legacy_etl.py`} {...WIN}>
        <div
          style={{
            position: "absolute",
            top: 20,
            left: 28 + codeX,
            transformOrigin: "0 0",
            transform: `translateY(${-scroll * scale}px) scale(${scale})`,
            fontFamily: mono,
            fontSize: CODE_FONT,
            lineHeight: `${LH}px`,
            whiteSpace: "pre",
            color: c.fg,
            opacity: dim,
          }}
        >
          {SECTIONS.map((s, i) => {
            const on = lerp(f, T.bands[0] + i * 22, T.bands[0] + i * 22 + 30, 0, 1);
            return (
              <div
                key={s.label}
                style={{
                  position: "absolute",
                  left: -40,
                  width: 2400,
                  top: (s.from - 1) * LH,
                  height: (s.to - s.from + 1) * LH,
                  background: s.color,
                  opacity: on * 0.28,
                  borderLeft: `${40}px solid ${s.color}`,
                }}
              />
            );
          })}
          {LEGACY.map((l, i) => (
            <div key={i} style={{ position: "relative", height: LH }}>
              <span style={{ display: "inline-block", width: 70, color: c.line, textAlign: "right", marginRight: 28 }}>{i + 1}</span>
              <Code line={l} kind="py" />
            </div>
          ))}
        </div>
      </Window>
      {/* labels next to the coloured bands, in screen space */}
      {SECTIONS.map((s, i) => {
        const on = spring({ frame: f - T.bands[0] - i * 22, fps: 60, config: { damping: 14, stiffness: 140 } });
        const fly = lerp(f, T.collapse[0] + i * 18, T.collapse[0] + i * 18 + 70, 0, 1);
        const y = WIN.y + 46 + 20 + ((s.from + s.to) / 2 - 1) * LH * FIT;
        return (
          <div
            key={s.label}
            style={{
              position: "absolute",
              left: WIN.x + 640,
              top: y - 22,
              opacity: on * (1 - fly),
              transform: `translateX(${(1 - on) * 60 + fly * 400}px)`,
              display: "flex",
              alignItems: "center",
              gap: 14,
              fontFamily: sans,
              fontSize: 30,
              fontWeight: 600,
              color: c.fg,
            }}
          >
            <div style={{ width: 16, height: 16, borderRadius: 8, background: s.color, boxShadow: `0 0 24px ${s.color}` }} />
            {s.label}
            <span style={{ fontFamily: mono, fontSize: 22, color: c.muted }}>{s.to - s.from + 1} lines</span>
          </div>
        );
      })}
      <Counter value={lines} />
    </div>
  );
};

const Counter: React.FC<{ value: number }> = ({ value }) => {
  const f = useCurrentFrame();
  const show = lerp(f, T.scroll[0], T.scroll[0] + 30, 0, 1) * (1 - lerp(f, T.collapse[0], T.collapse[0] + 30, 0, 1));
  return (
    <div style={{ position: "absolute", right: WIN.x + 30, bottom: 1080 - WIN.y - WIN.h + 26, opacity: show, textAlign: "right" }}>
      <div style={{ fontFamily: brand, fontWeight: 700, fontSize: 96, color: c.fg, lineHeight: 1 }}>{value}</div>
      <div style={{ fontFamily: sans, fontSize: 24, color: c.muted }}>lines of glue code</div>
    </div>
  );
};

// ------------------------------------------------------------------ captions
const Caption: React.FC<{ text: string; from: number; to: number; sub?: string }> = ({ text, from, to, sub }) => {
  const f = useCurrentFrame();
  const o = lerp(f, from, from + 25, 0, 1) * (1 - lerp(f, to - 25, to, 0, 1));
  return (
    <div style={{ position: "absolute", top: 30, left: 0, right: 0, textAlign: "center", opacity: o, transform: `translateY(${(1 - o) * 14}px)`, textShadow: `0 4px 30px ${c.bg}, 0 0 12px ${c.bg}` }}>
      <div style={{ fontFamily: sans, fontWeight: 700, fontSize: 44, color: c.fg, letterSpacing: -0.5 }}>{text}</div>
      {sub ? <div style={{ fontFamily: sans, fontSize: 24, color: c.muted, marginTop: 4 }}>{sub}</div> : null}
    </div>
  );
};

// ------------------------------------------------------------------ scene 4-5: the agentl pipeline
const YLH = 19;
const YAML_WIN = { x: 1030, y: 124, w: 790, h: 46 + 28 + PIPELINE.length * YLH + 16 };

const sectionOfYamlLine = (n: number) => SECTIONS.find((s) => s.yaml && n >= s.yaml[0] && n <= s.yaml[1]);

const Pipeline: React.FC = () => {
  const f = useCurrentFrame();
  const enter = lerp(f, T.collapse[0] + 60, T.collapse[0] + 160, 0, 1);
  const toLeft = lerp(f, T.run[0], T.run[0] + 70, 0, 1, inOut);
  const exit = lerp(f, T.end[0] - 10, T.end[0] + 30, 0, 1, inOut);
  const x = interpolate(toLeft, [0, 1], [0, -880]);
  const s = interpolate(toLeft, [0, 1], [1, 0.82]);
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        opacity: enter * (1 - exit),
        transform: `translateX(${x + (1 - enter) * 120}px) scale(${s})`,
        transformOrigin: `${YAML_WIN.x}px 50%`,
      }}
    >
      <Window title="pipeline.yaml" {...YAML_WIN}>
        <div style={{ padding: "16px 26px", fontFamily: mono, fontSize: 16, lineHeight: `${YLH}px`, whiteSpace: "pre", color: c.fg }}>
          {PIPELINE.map((l, i) => {
            const sec = sectionOfYamlLine(i + 1);
            const idx = sec ? SECTIONS.indexOf(sec) : 0;
            const at = T.collapse[0] + 110 + idx * 18 + (i % 6) * 3;
            const o = lerp(f, at, at + 20, 0, 1);
            const glow = sec ? lerp(f, at, at + 50, 1, 0) : 0;
            return (
              <div
                key={i}
                style={{
                  height: YLH,
                  opacity: o,
                  transform: `translateX(${(1 - o) * 30}px)`,
                  background: sec ? `${sec.color}${Math.round(glow * 60).toString(16).padStart(2, "0")}` : undefined,
                  borderLeft: sec ? `3px solid ${sec.color}` : "3px solid transparent",
                  paddingLeft: 12,
                  marginLeft: -15,
                }}
              >
                <Code line={l} kind="yaml" />
              </div>
            );
          })}
        </div>
      </Window>
      <Builtins />
      <BigCount />
    </div>
  );
};

const Builtins: React.FC = () => {
  const f = useCurrentFrame();
  const items = SECTIONS.filter((s) => s.builtin);
  return (
    <div style={{ position: "absolute", left: 120, top: 360, width: 800 }}>
      {items.map((s, i) => {
        const o = lerp(f, T.collapse[0] + 220 + i * 25, T.collapse[0] + 260 + i * 25, 0, 1);
        return (
          <div key={s.label} style={{ opacity: o, transform: `translateY(${(1 - o) * 20}px)`, marginBottom: 26 }}>
            <div style={{ fontFamily: sans, fontSize: 26, color: c.muted, textDecoration: "line-through", textDecorationColor: s.color }}>
              {s.label} ({s.to - s.from + 1} lines)
            </div>
            <div style={{ fontFamily: sans, fontSize: 30, fontWeight: 600, color: c.fg, marginTop: 6 }}>
              <span style={{ color: s.color }}>built in:</span> {s.builtin}
            </div>
          </div>
        );
      })}
    </div>
  );
};

const BigCount: React.FC = () => {
  const f = useCurrentFrame();
  const n = Math.round(lerp(f, T.collapse[0] + 120, T.collapse[0] + 330, LEGACY.length, PIPELINE.length, inOut));
  const o = lerp(f, T.collapse[0] + 100, T.collapse[0] + 140, 0, 1) * (1 - lerp(f, T.run[0], T.run[0] + 30, 0, 1));
  return (
    <div style={{ position: "absolute", left: 120, top: 150, opacity: o }}>
      <div style={{ fontFamily: brand, fontWeight: 700, fontSize: 150, lineHeight: 1, ...gradientText }}>{n}</div>
      <div style={{ fontFamily: sans, fontSize: 30, color: c.muted }}>lines of YAML</div>
    </div>
  );
};

// ------------------------------------------------------------------ scene 5: it runs
type Node = { id: string; kind: string; x: number; y: number; color: string };
const NODES: Node[] = [
  { id: "invoices", kind: "http.get", x: 0, y: 0, color: c.blue },
  { id: "customers", kind: "csv.read", x: 0, y: 210, color: c.blue },
  { id: "archive", kind: "json.write", x: 330, y: -150, color: c.blue },
  { id: "clean", kind: "pandas", x: 330, y: 105, color: c.teal },
  { id: "classify", kind: "agent", x: 600, y: 105, color: c.violet },
  { id: "save", kind: "postgres.write", x: 870, y: 105, color: c.green },
];
const EDGES: Array<[string, string]> = [
  ["invoices", "archive"],
  ["invoices", "clean"],
  ["customers", "clean"],
  ["clean", "classify"],
  ["classify", "save"],
];
const ORDER = ["invoices", "customers", "archive", "clean", "classify", "save"];
const NW = 220;
const NH = 86;
const G = { x: 760, y: 400 };

const LOG = [
  ["step.succeeded", "invoices", "records=1240 pages=3"],
  ["step.succeeded", "customers", "records=312"],
  ["step.succeeded", "archive", "s3://raw-invoices/…/raw.json"],
  ["step.succeeded", "clean", "records=1227"],
  ["item.rejected", "classify", "count=9 rate=0.007"],
  ["step.succeeded", "classify", "records=1218 cost_usd=0.41"],
  ["step.succeeded", "save", "upserted=1218"],
  ["run.succeeded", "", "seconds=38.2"],
];

const Run: React.FC = () => {
  const f = useCurrentFrame();
  const t0 = T.run[0] + 60;
  const o = lerp(f, T.run[0] + 20, T.run[0] + 70, 0, 1) * (1 - lerp(f, T.end[0] - 10, T.end[0] + 30, 0, 1));
  const doneAt = (id: string) => t0 + ORDER.indexOf(id) * 30;
  const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));
  return (
    <div style={{ position: "absolute", inset: 0, opacity: o }}>
      <svg style={{ position: "absolute", inset: 0 }} width={1920} height={1080}>
        {EDGES.map(([a, b]) => {
          const A = byId[a];
          const B = byId[b];
          const x1 = G.x + A.x + NW;
          const y1 = G.y + A.y + NH / 2;
          const x2 = G.x + B.x;
          const y2 = G.y + B.y + NH / 2;
          const p = lerp(f, doneAt(a), doneAt(b), 0, 1);
          const d = `M ${x1} ${y1} C ${x1 + 60} ${y1}, ${x2 - 60} ${y2}, ${x2} ${y2}`;
          return (
            <g key={a + b}>
              <path d={d} stroke={c.line} strokeWidth={3} fill="none" />
              <path d={d} stroke={B.color} strokeWidth={3} fill="none" pathLength={1} strokeDasharray="1 1" strokeDashoffset={1 - p} />
            </g>
          );
        })}
      </svg>
      {NODES.map((n) => {
        const on = spring({ frame: f - doneAt(n.id), fps: 60, config: { damping: 13, stiffness: 160 } });
        return (
          <div
            key={n.id}
            style={{
              position: "absolute",
              left: G.x + n.x,
              top: G.y + n.y,
              width: NW,
              height: NH,
              borderRadius: n.kind === "agent" ? 43 : 14,
              border: `2px solid ${on > 0.05 ? n.color : c.line}`,
              background: `${c.panel}`,
              boxShadow: on > 0.05 ? `0 0 ${40 * on}px ${n.color}88` : "none",
              transform: `scale(${1 + 0.06 * Math.sin(Math.min(1, on) * Math.PI)})`,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              fontFamily: sans,
            }}
          >
            <div style={{ fontSize: 27, fontWeight: 700, color: c.fg }}>{n.id}</div>
            <div style={{ fontFamily: mono, fontSize: 18, color: on > 0.05 ? n.color : c.muted }}>{n.kind}</div>
          </div>
        );
      })}
      <div
        style={{
          position: "absolute",
          left: G.x,
          top: 740,
          width: 1090,
          padding: "18px 24px",
          background: "#05060a",
          border: `1px solid ${c.line}`,
          borderRadius: 14,
          fontFamily: mono,
          fontSize: 20,
          lineHeight: "30px",
        }}
      >
        <div style={{ color: c.muted, display: "flex", justifyContent: "space-between" }}>
          <span>$ agentl run examples/invoices/pipeline.yaml</span>
          <span style={{ fontFamily: sans, fontSize: 16 }}>sample output</span>
        </div>
        {LOG.slice(Math.max(0, LOG.findIndex((_, i) => f < t0 + 24 + i * 26) - 5)).map(([type, step, data], i, arr) => {
          const idx = LOG.indexOf(arr[i]);
          const at = t0 + 24 + idx * 26;
          if (f < at) return null;
          const col = type === "item.rejected" ? c.amber : type === "run.succeeded" ? c.green : c.fg;
          return (
            <div key={idx} style={{ opacity: lerp(f, at, at + 10, 0, 1) }}>
              <span style={{ color: col }}>{type}</span>
              {step ? <span style={{ color: c.violet }}> [{step}]</span> : null}
              <span style={{ color: c.muted }}> {data}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

// ------------------------------------------------------------------ end card
const End: React.FC = () => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - T.end[0] - 20, fps, config: { damping: 14, stiffness: 120 } });
  const s2 = spring({ frame: f - T.end[0] - 60, fps, config: { damping: 16, stiffness: 120 } });
  const s3 = spring({ frame: f - T.end[0] - 95, fps, config: { damping: 16, stiffness: 120 } });
  return (
    <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 26 }}>
      <div style={{ fontFamily: sans, fontSize: 52, fontWeight: 700, color: c.fg, opacity: s, transform: `translateY(${(1 - s) * 30}px)` }}>
        <span style={{ color: c.muted }}>{LEGACY.length} lines of glue</span> → {PIPELINE.length} lines of YAML
      </div>
      <div style={{ fontFamily: brand, fontWeight: 700, fontSize: 210, lineHeight: 1, ...gradientText, opacity: s2, transform: `scale(${0.85 + 0.15 * s2})` }}>
        agentl
      </div>
      <div style={{ display: "flex", gap: 28, opacity: s3, transform: `translateY(${(1 - s3) * 20}px)` }}>
        <div style={{ fontFamily: mono, fontSize: 30, color: c.fg, padding: "14px 26px", border: `1px solid ${c.line}`, borderRadius: 12, background: c.panel }}>
          pip install agentl-etl
        </div>
        <div style={{ fontFamily: sans, fontSize: 30, color: c.muted, padding: "14px 0" }}>agentl.com.br</div>
      </div>
      <div style={{ fontFamily: sans, fontSize: 22, color: c.muted, opacity: s3, marginTop: 10 }}>
        Same pipeline, from examples/invoices. Your pandas code stays as is.
      </div>
    </AbsoluteFill>
  );
};

// ------------------------------------------------------------------ composition
export const Compress: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{ fontFamily: sans }}>
      <Backdrop />
      {f < T.run[0] + 60 ? <Legacy /> : null}
      {f >= T.collapse[0] && f < T.end[0] + 40 ? <Pipeline /> : null}
      {f >= T.run[0] && f < T.end[0] + 40 ? <Run /> : null}
      {f >= T.end[0] ? <End /> : null}
      <Caption text="A real-world ETL in Python." sub="examples/invoices/legacy_etl.py" from={20} to={T.zoom[0] + 20} />
      <Caption text="Most of it is plumbing." from={T.bands[0]} to={T.collapse[0] + 20} />
      <Caption text="agentl keeps the logic. The plumbing is built in." from={T.collapse[0] + 40} to={T.run[0] + 20} />
      <Caption text="Same job, now with retries, cache, events and a dashboard." from={T.run[0] + 30} to={T.end[0]} />
    </AbsoluteFill>
  );
};
