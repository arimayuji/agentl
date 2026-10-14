import React from "react";
import { AbsoluteFill, Easing, interpolate, spring, useCurrentFrame } from "remotion";
import { brand, c, mono, sans } from "./theme";

// Looping clips (1280x720, 30 fps, 8 s) that show one feature each in the docs. Every clip keeps
// something moving the whole time (records flowing, counters, bars) and fills the frame: the YAML
// on the left, a stage on the right. The poster is taken mid-action (see render-clips.sh).
export const CLIP_FPS = 30;
export const CLIP_FRAMES = 240;
export const CLIP_W = 1280;
export const CLIP_H = 720;

const STAGE_W = 740;
const STAGE_H = 584;
const red = "#fb7185";

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const lerp = (f: number, a: number, b: number, x: number, y: number) =>
  interpolate(f, [a, b], [x, y], { ...clamp, easing: ease });
const lin = (f: number, a: number, b: number, x: number, y: number) => interpolate(f, [a, b], [x, y], clamp);
const pop = (f: number, at: number) =>
  Math.min(1, spring({ frame: f - at, fps: CLIP_FPS, config: { damping: 14, stiffness: 160 } }));
/** fade the whole clip in and out so the loop is seamless */
const loopFade = (f: number) => Math.min(lerp(f, 0, 8, 0, 1), 1 - lerp(f, CLIP_FRAMES - 10, CLIP_FRAMES, 0, 1));
const fmt = (n: number) => Math.round(n).toLocaleString("en-US");

type P = [number, number];
/** point on a cubic bezier from a to b, bending horizontally */
const curve = (a: P, b: P, t: number): P => {
  const mx = (a[0] + b[0]) / 2;
  const p1: P = [mx, a[1]];
  const p2: P = [mx, b[1]];
  const u = 1 - t;
  return [
    u * u * u * a[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * b[0],
    u * u * u * a[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * b[1],
  ];
};
const curvePath = (a: P, b: P) => {
  const mx = (a[0] + b[0]) / 2;
  return `M ${a[0]} ${a[1]} C ${mx} ${a[1]}, ${mx} ${b[1]}, ${b[0]} ${b[1]}`;
};

/** A wire with records travelling along it while `from <= f < to`. */
const Wire: React.FC<{ a: P; b: P; color: string; from?: number; to?: number; every?: number; travel?: number; seed?: number }> = ({
  a,
  b,
  color,
  from = 0,
  to = CLIP_FRAMES,
  every = 9,
  travel = 34,
  seed = 0,
}) => {
  const f = useCurrentFrame();
  const dots: React.ReactNode[] = [];
  for (let start = from + (seed % every); start < to; start += every) {
    const t = (f - start) / travel;
    if (t < 0 || t > 1) continue;
    const [x, y] = curve(a, b, Easing.inOut(Easing.quad)(t));
    const o = Math.min(1, t * 6, (1 - t) * 6);
    dots.push(<circle key={start} cx={x} cy={y} r={5} fill={color} opacity={o} style={{ filter: `drop-shadow(0 0 6px ${color})` }} />);
  }
  const on = f >= from && f < to + travel;
  return (
    <g>
      <path d={curvePath(a, b)} stroke={on ? `${color}66` : c.line} strokeWidth={2} fill="none" />
      {dots}
    </g>
  );
};

const YAML_RE = /(#.*$)|(\$\{[^}]+\})|("[^"]*")|(^\s*-?\s*\$?\s*[\w.]+(?=:))/g;
const Yaml: React.FC<{ lines: string[] }> = ({ lines }) => {
  const f = useCurrentFrame();
  return (
    <div style={{ fontFamily: mono, fontSize: 20, lineHeight: "34px", whiteSpace: "pre", color: c.fg }}>
      {lines.map((l, i) => {
        const o = lerp(f, 4 + i * 2, 14 + i * 2, 0, 1);
        const parts: React.ReactNode[] = [];
        let last = 0;
        let k = 0;
        for (const m of l.matchAll(YAML_RE)) {
          if (m.index! > last) parts.push(l.slice(last, m.index));
          const col = m[1] ? c.muted : m[2] ? c.amber : m[3] ? c.green : c.blue;
          parts.push(
            <span key={k++} style={{ color: col }}>
              {m[0]}
            </span>,
          );
          last = m.index! + m[0].length;
        }
        parts.push(l.slice(last) || "\u00a0");
        return (
          <div key={i} style={{ opacity: o, transform: `translateX(${(1 - o) * 14}px)` }}>
            {parts}
          </div>
        );
      })}
    </div>
  );
};

const Frame: React.FC<{ title: string; yaml: string[]; note: string; children: React.ReactNode }> = ({ title, yaml, note, children }) => {
  const f = useCurrentFrame();
  const drift = Math.sin((f / CLIP_FRAMES) * Math.PI * 2);
  return (
    <AbsoluteFill style={{ background: c.bg, fontFamily: sans, opacity: loopFade(f) }}>
      <AbsoluteFill
        style={{
          background: `radial-gradient(640px 460px at ${18 + drift * 6}% ${22 + drift * 4}%, ${c.indigo}26, transparent 70%), radial-gradient(620px 520px at ${84 - drift * 5}% ${78 - drift * 4}%, ${c.teal}1c, transparent 70%)`,
        }}
      />
      <div style={{ position: "absolute", left: 40, top: 30, fontSize: 30, fontWeight: 700, color: c.fg }}>
        <span style={{ fontFamily: brand, color: c.violet }}>agentl</span> · {title}
      </div>
      <div style={{ position: "absolute", right: 40, top: 40, fontSize: 17, color: c.muted, maxWidth: 560, textAlign: "right" }}>{note}</div>
      <div
        style={{
          position: "absolute",
          left: 40,
          top: 96,
          width: 420,
          bottom: 40,
          padding: "0 26px",
          background: `${c.panel}ee`,
          border: `1px solid ${c.line}`,
          borderRadius: 16,
          display: "flex",
          alignItems: "center",
          overflow: "hidden",
        }}
      >
        <Yaml lines={yaml} />
      </div>
      <div style={{ position: "absolute", left: 500, top: 96, width: STAGE_W, height: STAGE_H }}>{children}</div>
    </AbsoluteFill>
  );
};

const Node: React.FC<{ x: number; y: number; w?: number; label: string; sub?: string; color: string; glow?: number; round?: boolean }> = ({
  x,
  y,
  w = 170,
  label,
  sub,
  color,
  glow = 0,
  round,
}) => (
  <div
    style={{
      position: "absolute",
      left: x - w / 2,
      top: y - 34,
      width: w,
      height: 68,
      borderRadius: round ? 34 : 14,
      border: `2px solid ${color}`,
      background: `${c.panel}`,
      boxShadow: `0 0 ${10 + glow * 30}px ${color}${glow > 0.5 ? "aa" : "55"}`,
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      color: c.fg,
    }}
  >
    <div style={{ fontSize: 20, fontWeight: 700 }}>{label}</div>
    {sub ? <div style={{ fontSize: 13, color: c.muted, fontFamily: mono, marginTop: 2 }}>{sub}</div> : null}
  </div>
);

const Chip: React.FC<{ text: string; color: string; style?: React.CSSProperties }> = ({ text, color, style }) => (
  <div
    style={{
      display: "inline-flex",
      alignItems: "center",
      padding: "8px 14px",
      borderRadius: 11,
      border: `1.5px solid ${color}`,
      background: `${color}1f`,
      color: c.fg,
      fontFamily: mono,
      fontSize: 17,
      whiteSpace: "nowrap",
      ...style,
    }}
  >
    {text}
  </div>
);

const Big: React.FC<{ value: string; label: string; color?: string; size?: number }> = ({ value, label, color = c.fg, size = 64 }) => (
  <div>
    <div style={{ fontFamily: brand, fontSize: size, lineHeight: 1, color, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    <div style={{ fontSize: 17, color: c.muted, marginTop: 6 }}>{label}</div>
  </div>
);

const Bar: React.FC<{ value: number; color: string; width?: number; height?: number }> = ({ value, color, width = STAGE_W, height = 16 }) => (
  <div style={{ width, height, borderRadius: height / 2, background: c.line, overflow: "hidden" }}>
    <div style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%`, height: "100%", background: color, borderRadius: height / 2 }} />
  </div>
);

/** Rows that slide in from the top of a list, newest first; `items` is the full list in order. */
const Feed: React.FC<{ items: { at: number; node: React.ReactNode }[]; max: number; rowH?: number }> = ({ items, max, rowH = 44 }) => {
  const f = useCurrentFrame();
  const shown = items.filter((it) => f >= it.at).slice(-max).reverse();
  return (
    <div style={{ position: "relative", height: max * rowH, overflow: "hidden" }}>
      {shown.map((it, i) => {
        const p = pop(f, it.at);
        return (
          <div key={it.at} style={{ position: "absolute", left: 0, right: 0, top: i * rowH, height: rowH, opacity: i === 0 ? p : 1, transform: i === 0 ? `translateY(${(p - 1) * 20}px)` : undefined }}>
            {it.node}
          </div>
        );
      })}
    </div>
  );
};

const Cells: React.FC<{ cells: React.ReactNode[]; widths: number[]; color?: string; style?: React.CSSProperties }> = ({ cells, widths, color = c.fg, style }) => (
  <div style={{ display: "flex", alignItems: "center", height: "100%", fontFamily: mono, fontSize: 17, color, borderBottom: `1px solid ${c.line}`, ...style }}>
    {cells.map((x, i) => (
      <div key={i} style={{ width: widths[i], overflow: "hidden", whiteSpace: "nowrap", paddingRight: 10 }}>
        {x}
      </div>
    ))}
  </div>
);

// ---------------------------------------------------------------- 1. file.read
export const ClipFiles: React.FC = () => {
  const f = useCurrentFrame();
  const files = [
    ["sales.csv", c.teal, "120.00"],
    ["q3.xlsx", c.green, "88.50"],
    ["nota.pdf", red, "text…"],
    ["batch.zip!a.csv", c.amber, "45.10"],
    ["events.jsonl.gz", c.blue, "12.00"],
  ] as const;
  const hub: P = [330, 150];
  const records = Math.floor(lin(f, 30, 200, 0, 48210));
  const rows = Array.from({ length: 30 }, (_, i) => {
    const [name, col, total] = files[i % files.length];
    return {
      at: 46 + i * 5,
      node: <Cells widths={[260, 110, 140]} cells={[<span style={{ color: col }}>{name}</span>, String(1000 + i * 7), total]} />,
    };
  });
  return (
    <Frame title="read any file" note="Format by extension. Zip and gz expanded safely. Local, URL, S3, GCS, Azure, SFTP." yaml={["- id: inbox", "  use: file.read", "  with:", "    path: s3://bucket/inbox/**/*", "", "- id: clean", "  use: select", "  input: inbox"]}>
      <svg width={STAGE_W} height={300} style={{ position: "absolute", left: 0, top: 0 }}>
        {files.map(([, col], i) => (
          <Wire key={i} a={[150, 30 + i * 60]} b={[hub[0] - 85, hub[1]]} color={col} from={18 + i * 4} to={200} every={11} seed={i * 3} />
        ))}
        <Wire a={[hub[0] + 85, hub[1]]} b={[600, hub[1]]} color={c.violet} from={40} to={205} every={5} travel={24} />
      </svg>
      {files.map(([name, col], i) => (
        <div key={name} style={{ position: "absolute", left: 0, top: 30 + i * 60 - 20, opacity: pop(f, 8 + i * 4) }}>
          <Chip text={name} color={col} style={{ width: 150, padding: "8px 10px", fontSize: 15, justifyContent: "center" }} />
        </div>
      ))}
      <Node x={hub[0]} y={hub[1]} label="file.read" sub="detect · expand" color={c.violet} glow={(Math.sin(f / 5) + 1) / 2} />
      <div style={{ position: "absolute", left: 610, top: 100 }}>
        <Big value={fmt(records)} label="records" size={44} />
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 330 }}>
        <Cells widths={[260, 110, 140]} cells={["_file", "id", "total"]} color={c.muted} style={{ height: 36 }} />
        <Feed items={rows} max={5} />
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 2. HTTP pagination
export const ClipHttp: React.FC = () => {
  const f = useCurrentFrame();
  const pages = [
    { p: 1, at: 20, dur: 26 },
    { p: 2, at: 50, dur: 24, retry: true },
    { p: 3, at: 104, dur: 26 },
    { p: 4, at: 134, dur: 24, last: true },
  ];
  const got = pages.reduce((n, pg) => n + (f > pg.at + pg.dur + (pg.retry ? 30 : 0) ? 500 : 0), 0);
  const shown = Math.round(lerp(f, 30, 180, 0, 2000));
  return (
    <Frame title="APIs with pagination" note="Retries 429 and 5xx with backoff. Internal addresses blocked by default." yaml={["- id: invoices", "  use: http.get", "  with:", "    url: ${API}/invoices", "    params: { per_page: 500 }", "    paginate:", "      next: next_page", "    items: items", "    concurrency: 8"]}>
      <div style={{ fontSize: 17, color: c.muted, marginBottom: 14 }}>requests</div>
      {pages.map((pg, i) => {
        const fail = pg.retry ? lin(f, pg.at, pg.at + 12, 0, 1) : 0;
        const retryAt = pg.at + 30;
        const ok = lin(f, pg.retry ? retryAt : pg.at, (pg.retry ? retryAt : pg.at) + pg.dur, 0, 1);
        const failing = pg.retry && f >= pg.at && f < retryAt;
        const done = ok >= 1;
        return (
          <div key={pg.p} style={{ display: "flex", alignItems: "center", gap: 16, height: 78, opacity: pop(f, pg.at - 6) }}>
            <Chip text={`GET ?page=${pg.p}`} color={c.blue} style={{ width: 170 }} />
            <div style={{ position: "relative", width: 330, height: 14, borderRadius: 7, background: c.line, overflow: "hidden" }}>
              {failing ? <div style={{ width: `${fail * 40}%`, height: "100%", background: red }} /> : <div style={{ width: `${ok * 100}%`, height: "100%", background: c.green }} />}
            </div>
            <div style={{ fontFamily: mono, fontSize: 17, color: failing ? red : done ? c.green : c.muted, width: 220 }}>
              {failing ? "503 · retry in 1s" : done ? `200 · 500 items${pg.last ? " · end" : ""}` : "…"}
            </div>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 70, marginTop: 60 }}>
        <Big value={fmt(Math.min(shown, got || shown))} label="records" size={80} />
        <Big value={String(pages.filter((pg) => f > pg.at).length + (f > 62 ? 1 : 0))} label="requests" color={c.blue} />
        <Big value={f > 62 ? "1" : "0"} label="retried" color={c.amber} />
      </div>
      <div style={{ marginTop: 50 }}>
        <Bar value={lin(f, 20, 175, 0, 1)} color={`linear-gradient(90deg, ${c.blue}, ${c.teal})`} />
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 3. agent step with validation
export const ClipAgent: React.FC = () => {
  const f = useCurrentFrame();
  const items = [
    ["AWS bill Sep", "software", "312.40", true],
    ["Uber to client", "travel", "38.90", true],
    ["Figma seats", "software", "90.00", true],
    ["???? scan", "\"lots\"", "—", false],
    ["Hotel Lisbon", "travel", "410.00", true],
    ["Team lunch", "meals", "126.30", true],
    ["Notion plan", "software", "48.00", true],
    ["Cab airport", "travel", "52.10", true],
    ["unreadable", "?", "—", false],
    ["Coffee beans", "office", "34.90", true],
  ] as const;
  const step = 15;
  const ok = items.filter((_, i) => f > 50 + i * step && items[i][3]).length;
  const bad = items.filter((_, i) => f > 50 + i * step && !items[i][3]).length;
  const rows = items.map(([d, cat, amt, valid], i) => ({
    at: 50 + i * step,
    node: (
      <Cells
        widths={[220, 140, 110, 160]}
        color={valid ? c.fg : c.amber}
        cells={[d, cat, amt, <span style={{ fontFamily: sans, fontSize: 15, color: valid ? c.green : c.amber }}>{valid ? "✓ valid" : "retry → rejected"}</span>]}
      />
    ),
  }));
  return (
    <Frame title="agent steps with a schema" note="Every answer is validated. One retry, then the record is rejected, not guessed." yaml={["- id: classify", "  agent: Classify the expense", "    on this invoice line.", "  input: invoices", "  concurrency: 8", "  output:", "    category: str", "    amount: float", "    needs_review: bool"]}>
      <svg width={STAGE_W} height={190} style={{ position: "absolute", left: 0, top: 0 }}>
        <Wire a={[90, 90]} b={[285, 90]} color={c.blue} from={14} to={200} every={step} travel={30} />
        <Wire a={[455, 90]} b={[650, 40]} color={c.green} from={44} to={210} every={step} travel={26} />
        <Wire a={[455, 90]} b={[650, 150]} color={c.amber} from={80} to={81} every={50} travel={26} />
        <Wire a={[455, 90]} b={[650, 150]} color={c.amber} from={155} to={156} every={50} travel={26} />
      </svg>
      <Node x={45} y={90} w={90} label="in" color={c.blue} />
      <Node x={370} y={90} label="agent" sub="schema ✓" color={c.violet} round glow={(Math.sin(f / 4) + 1) / 2} />
      <Node x={690} y={40} w={100} label={String(ok)} sub="valid" color={c.green} />
      <Node x={690} y={150} w={100} label={String(bad)} sub="rejected" color={c.amber} />
      <div style={{ position: "absolute", left: 0, right: 0, top: 216 }}>
        <Cells widths={[220, 140, 110, 160]} cells={["description", "category", "amount", ""]} color={c.muted} style={{ height: 36 }} />
        <Feed items={rows} max={7} />
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 4. cache
export const ClipCache: React.FC = () => {
  const f = useCurrentFrame();
  const run1 = lin(f, 16, 110, 0, 1);
  const run2 = lin(f, 130, 140, 0, 1);
  const fresh = lin(f, 140, 175, 0, 1);
  return (
    <Frame title="per-record cache" note="Re-runs and retries only pay for records that changed. SQLite locally, Postgres when shared." yaml={["- id: classify", "  agent: Classify the expense", "  input: invoices", "  cache: true   # default", "  output:", "    category: str"]}>
      {[
        { label: "run 1 · 1,000 records", calls: run1 * 1000, cost: run1 * 0.41, fill: run1, color: c.violet, cached: 0, at: 6 },
        { label: "run 2 · 1,003 records (3 new)", calls: fresh * 3, cost: fresh * 0.0012, fill: run2 * 0.997 + fresh * 0.003, color: c.green, cached: run2 * 1000, at: 122 },
      ].map((r, i) => (
        <div key={i} style={{ marginBottom: 46, opacity: pop(f, r.at) }}>
          <div style={{ fontSize: 22, color: c.fg, fontWeight: 700, marginBottom: 14 }}>{r.label}</div>
          <Bar value={r.fill} color={i ? `linear-gradient(90deg, ${c.green}88, ${c.green})` : `linear-gradient(90deg, ${c.indigo}, ${c.violet})`} height={22} />
          <div style={{ display: "flex", gap: 60, marginTop: 20 }}>
            <Big value={fmt(r.calls)} label="LLM calls" color={r.color} size={52} />
            <Big value={`$${r.cost.toFixed(r.cost < 0.1 && i ? 4 : 2)}`} label="cost" color={r.color} size={52} />
            {i ? <Big value={fmt(r.cached)} label="from cache" color={c.teal} size={52} /> : null}
          </div>
        </div>
      ))}
    </Frame>
  );
};

// ---------------------------------------------------------------- 5. budget
export const ClipBudget: React.FC = () => {
  const f = useCurrentFrame();
  const stopAt = 150;
  const spent = Math.min(5, lin(f, 14, stopAt, 0, 5));
  const hit = f >= stopAt;
  const calls = Math.floor(lin(f, 14, stopAt, 0, 1612));
  const R = 150;
  const arc = (v: number) => {
    const a = Math.PI * (1 - v);
    return [200 + R * Math.cos(a), 190 - R * Math.sin(a)];
  };
  const [ex, ey] = arc(spent / 5);
  const flash = hit ? (Math.sin((f - stopAt) / 2.5) + 1) / 2 : 0;
  const steps = [
    ["classify", 3.1, c.violet],
    ["summarize", 1.4, c.teal],
    ["tag", 0.5, c.blue],
  ] as const;
  return (
    <Frame title="cost budget" note="The run stops before the next call that would go over. Spend per step on the dashboard." yaml={["budget:", "  max_usd: 5.00", "  max_tokens: 2000000", "", "llm:", "  default:", "    provider: anthropic", "    price:", "      input: 3", "      output: 15"]}>
      <svg width={400} height={220} style={{ position: "absolute", left: 0, top: 10 }}>
        <path d={`M 50 190 A ${R} ${R} 0 0 1 350 190`} stroke={c.line} strokeWidth={22} fill="none" strokeLinecap="round" />
        {spent > 0.01 ? (
          <path d={`M 50 190 A ${R} ${R} 0 0 1 ${ex} ${ey}`} stroke={hit ? red : "url(#g)"} strokeWidth={22} fill="none" strokeLinecap="round" />
        ) : null}
        <defs>
          <linearGradient id="g" x1="0" x2="1">
            <stop offset="0" stopColor={c.teal} />
            <stop offset="1" stopColor={c.amber} />
          </linearGradient>
        </defs>
      </svg>
      <div style={{ position: "absolute", left: 0, width: 400, top: 120, textAlign: "center" }}>
        <div style={{ fontFamily: brand, fontSize: 64, color: hit ? red : c.fg }}>${spent.toFixed(2)}</div>
        <div style={{ fontSize: 17, color: c.muted }}>of $5.00</div>
      </div>
      <div style={{ position: "absolute", left: 440, top: 40 }}>
        <Big value={fmt(calls)} label="model calls" size={52} />
        <div style={{ height: 30 }} />
        <Big value={hit ? "stopped" : "running"} label="run status" color={hit ? red : c.green} size={40} />
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 290 }}>
        {steps.map(([name, share, col], i) => (
          <div key={name} style={{ display: "flex", alignItems: "center", gap: 18, height: 50 }}>
            <div style={{ width: 120, fontFamily: mono, fontSize: 17, color: c.fg }}>{name}</div>
            <Bar value={(spent / 5) * (share / 3.1)} color={col} width={460} height={14} />
            <div style={{ fontFamily: mono, fontSize: 17, color: c.muted }}>${((spent / 5) * share).toFixed(2)}</div>
          </div>
        ))}
        <div style={{ marginTop: 22, opacity: lerp(f, stopAt, stopAt + 8, 0, 1) }}>
          <Chip text="budget.exceeded · max_usd 5.0 reached · notified #oncall" color={red} style={{ boxShadow: `0 0 ${12 + flash * 24}px ${red}88` }} />
        </div>
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 6. notifications
export const ClipNotify: React.FC = () => {
  const f = useCurrentFrame();
  const channels = [
    ["Slack", c.violet],
    ["Teams", c.blue],
    ["Discord", c.indigo],
    ["Telegram", c.teal],
    ["Email", c.green],
    ["PagerDuty", red],
    ["Opsgenie", c.amber],
  ] as const;
  const events = [
    { at: 12, text: "run.failed · step load", color: red },
    { at: 120, text: "item.rejected · rate 0.08", color: c.amber },
  ];
  const src: P = [150, 290];
  return (
    <Frame title="notifications" note="Rules per event. Deduplicated per pipeline and step. A failing channel never fails the run." yaml={["notify:", "  channels:", "    oncall: { type: pagerduty }", "    team: { type: slack }", "  rules:", "    - on: [run.failed]", "      to: [oncall, team]", "    - on: item.rejected", "      when: \"rate > 0.05\"", "      to: [mail]"]}>
      <svg width={STAGE_W} height={STAGE_H} style={{ position: "absolute", left: 0, top: 0 }}>
        {channels.map(([, col], i) => {
          const y = 40 + i * 78;
          const target = i === 4 ? [events[1]] : [events[0]];
          return target.map((ev) => <Wire key={`${i}-${ev.at}`} a={[src[0] + 120, src[1]]} b={[520, y]} color={col} from={ev.at + 10} to={ev.at + 40} every={10} travel={26} seed={i} />);
        })}
      </svg>
      {events.map((ev) => {
        const on = f >= ev.at && (ev.at === 120 || f < 120);
        return (
          <div key={ev.at} style={{ position: "absolute", left: 0, top: src[1] - 26, opacity: on ? pop(f, ev.at) : 0 }}>
            <Chip text={ev.text} color={ev.color} style={{ width: 240, justifyContent: "center", fontSize: 15 }} />
          </div>
        );
      })}
      {channels.map(([name, col], i) => {
        const y = 40 + i * 78;
        const at = i === 4 ? 160 : 50 + i * 3;
        const delivered = f > at;
        return (
          <div key={name} style={{ position: "absolute", left: 530, top: y - 24, display: "flex", alignItems: "center", gap: 12 }}>
            <Chip text={name} color={col} style={{ width: 130, justifyContent: "center", boxShadow: delivered ? `0 0 ${14 * pop(f, at)}px ${col}88` : "none" }} />
            <div style={{ fontSize: 15, color: delivered ? c.green : c.muted, opacity: delivered ? pop(f, at) : 0.5 }}>{delivered ? "✓ sent" : "·"}</div>
          </div>
        );
      })}
    </Frame>
  );
};

// ---------------------------------------------------------------- 7. upsert
export const ClipUpsert: React.FC = () => {
  const f = useCurrentFrame();
  const run2 = f >= 120;
  const table = [
    ["INV-1", "120.00", "120.00"],
    ["INV-2", "80.00", "99.90"],
    ["INV-3", "45.10", "45.10"],
    ["INV-4", "310.00", "310.00"],
    ["INV-5", "18.40", "18.40"],
  ] as const;
  const incoming = (at: number) => (i: number) => lin(f, at + i * 10, at + i * 10 + 22, 0, 1);
  const in1 = incoming(14);
  const in2 = incoming(124);
  return (
    <Frame title="idempotent loads" note="Same file twice: no duplicates. Postgres, MySQL, SQL Server, Snowflake, BigQuery and more." yaml={["- id: save", "  use: postgres.write", "  input: invoices", "  with:", "    dsn: ${DATABASE_URL}", "    table: invoices", "    mode: upsert", "    key: number"]}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 18 }}>
        <div style={{ fontSize: 22, fontWeight: 700, color: c.fg }}>{run2 ? "run 2: same file, INV-2 changed" : "run 1: first load"}</div>
        <Chip text={run2 ? "ON CONFLICT (number) DO UPDATE" : "INSERT"} color={run2 ? c.green : c.blue} />
      </div>
      <Cells widths={[200, 200, 260]} cells={["number", "total", "status"]} color={c.muted} style={{ height: 40 }} />
      {table.map(([n, t1, t2], i) => {
        const p1 = in1(i);
        const p2 = in2(i);
        const changed = t1 !== t2;
        const flash = run2 ? Math.max(0, 1 - Math.abs(p2 - 0.8) * 3) : 0;
        const status = run2 ? (p2 > 0.8 ? (changed ? "updated" : "unchanged") : "") : p1 > 0.8 ? "inserted" : "";
        return (
          <div key={n} style={{ height: 56, opacity: p1, transform: `translateX(${(1 - p1) * -40}px)`, background: flash ? `${changed ? c.green : c.blue}${Math.round(flash * 40).toString(16).padStart(2, "0")}` : undefined }}>
            <Cells
              widths={[200, 200, 260]}
              color={run2 && changed && p2 > 0.8 ? c.green : c.fg}
              cells={[n, run2 && p2 > 0.8 ? t2 : t1, <span style={{ fontFamily: sans, fontSize: 15, color: status === "updated" ? c.green : c.muted }}>{status}</span>]}
            />
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 70, marginTop: 34 }}>
        <Big value="5" label="rows in the table" />
        <Big value={String(table.filter((_, i) => in1(i) > 0.8).length + table.filter((_, i) => in2(i) > 0.8).length)} label="records loaded" color={c.blue} />
        <Big value="0" label="duplicates" color={c.green} />
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 8. observability
export const ClipObserve: React.FC = () => {
  const f = useCurrentFrame();
  const steps = [
    { name: "extract", color: c.blue, start: 10, end: 60 },
    { name: "clean", color: c.teal, start: 50, end: 95 },
    { name: "classify", color: c.violet, start: 80, end: 175 },
    { name: "save", color: c.green, start: 165, end: 205 },
  ];
  const xs = [80, 270, 460, 650];
  const logs = [
    [16, "info", 'run.started {"steps": 4}'],
    [60, "info", 'step.succeeded extract {"records": 12400}'],
    [95, "info", 'step.succeeded clean {"records": 12391}'],
    [128, "warning", 'item.rejected classify {"count": 9, "rate": 0.001}'],
    [175, "info", 'step.succeeded classify {"cost_usd": 4.12}'],
    [205, "info", 'step.succeeded save {"records": 1}'],
    [212, "info", 'run.succeeded {"seconds": 38.2}'],
  ] as const;
  const span0 = 10;
  const span1 = 210;
  const px = (t: number) => ((t - span0) / (span1 - span0)) * (STAGE_W - 120);
  return (
    <Frame title="dashboard, logs and traces" note="A trace per run, a span per step, Prometheus metrics, JSON logs, Sentry for failures." yaml={["$ agentl run p.yaml \\", "    --otel \\", "    --log-format json", "", "$ agentl dashboard", "  http://127.0.0.1:7070"]}>
      <svg width={STAGE_W} height={110} style={{ position: "absolute", left: 0, top: 0 }}>
        {steps.slice(0, -1).map((s, i) => (
          <Wire key={s.name} a={[xs[i] + 70, 50]} b={[xs[i + 1] - 70, 50]} color={s.color} from={s.start + 6} to={steps[i + 1].end - 6} every={7} travel={18} />
        ))}
      </svg>
      {steps.map((s, i) => {
        const running = f >= s.start && f < s.end;
        const done = f >= s.end;
        return (
          <Node
            key={s.name}
            x={xs[i]}
            y={50}
            w={140}
            label={s.name}
            sub={done ? "✓ done" : running ? "running…" : "waiting"}
            color={done || running ? s.color : c.line}
            glow={running ? (Math.sin(f / 3) + 1) / 2 : 0}
            round={s.name === "classify"}
          />
        );
      })}
      <div style={{ position: "absolute", left: 0, right: 0, top: 120 }}>
        <div style={{ fontSize: 15, color: c.muted, marginBottom: 8 }}>trace</div>
        {steps.map((s) => {
          const w = Math.max(0, px(Math.min(f, s.end)) - px(s.start));
          return (
            <div key={s.name} style={{ display: "flex", alignItems: "center", height: 30 }}>
              <div style={{ width: 120, fontFamily: mono, fontSize: 14, color: c.muted }}>{s.name}</div>
              <div style={{ position: "relative", flex: 1, height: 16 }}>
                <div style={{ position: "absolute", left: px(s.start), width: f >= s.start ? w : 0, height: 16, borderRadius: 4, background: s.color, opacity: 0.85 }} />
              </div>
            </div>
          );
        })}
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, top: 290, padding: "14px 18px", background: `${c.panel}cc`, border: `1px solid ${c.line}`, borderRadius: 12 }}>
        <div style={{ fontSize: 15, color: c.muted, marginBottom: 6 }}>logs</div>
        <Feed
          max={7}
          rowH={34}
          items={logs.map(([at, level, text]) => ({
            at,
            node: (
              <div style={{ fontFamily: mono, fontSize: 15, lineHeight: "34px", whiteSpace: "nowrap", color: level === "warning" ? c.amber : c.fg }}>
                <span style={{ color: level === "warning" ? c.amber : c.green }}>{level.padEnd(7)}</span> {text}
              </div>
            ),
          }))}
        />
      </div>
    </Frame>
  );
};

export const CLIPS = [
  ["ClipFiles", ClipFiles],
  ["ClipHttp", ClipHttp],
  ["ClipAgent", ClipAgent],
  ["ClipCache", ClipCache],
  ["ClipBudget", ClipBudget],
  ["ClipNotify", ClipNotify],
  ["ClipUpsert", ClipUpsert],
  ["ClipObserve", ClipObserve],
] as const;
