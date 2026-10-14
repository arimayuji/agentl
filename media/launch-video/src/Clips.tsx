import React from "react";
import { AbsoluteFill, Easing, interpolate, spring, useCurrentFrame } from "remotion";
import { brand, c, mono, sans } from "./theme";

// Short looping clips (1280x720, 30 fps, 6 s) that show one feature each in the docs.
export const CLIP_FPS = 30;
export const CLIP_FRAMES = 180;
export const CLIP_W = 1280;
export const CLIP_H = 720;

const ease = Easing.bezier(0.16, 1, 0.3, 1);
const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const lerp = (f: number, a: number, b: number, x: number, y: number) =>
  interpolate(f, [a, b], [x, y], { ...clamp, easing: ease });
const pop = (f: number, at: number) => spring({ frame: f - at, fps: CLIP_FPS, config: { damping: 14, stiffness: 150 } });
/** fade the whole clip in and out so the loop is seamless */
const loopFade = (f: number) => Math.min(lerp(f, 0, 10, 0, 1), 1 - lerp(f, CLIP_FRAMES - 12, CLIP_FRAMES, 0, 1));

const YAML_RE = /(#.*$)|(\$\{[^}]+\})|("[^"]*")|(^\s*-?\s*[\w]+(?=:))/g;
const Yaml: React.FC<{ lines: string[] }> = ({ lines }) => {
  const f = useCurrentFrame();
  return (
    <div style={{ fontFamily: mono, fontSize: 19, lineHeight: "29px", whiteSpace: "pre", color: c.fg }}>
      {lines.map((l, i) => {
        const o = lerp(f, 6 + i * 3, 16 + i * 3, 0, 1);
        const parts: React.ReactNode[] = [];
        let last = 0;
        let k = 0;
        for (const m of l.matchAll(YAML_RE)) {
          if (m.index! > last) parts.push(l.slice(last, m.index));
          const col = m[1] ? c.muted : m[2] ? c.amber : m[3] ? c.green : c.blue;
          parts.push(<span key={k++} style={{ color: col }}>{m[0]}</span>);
          last = m.index! + m[0].length;
        }
        parts.push(l.slice(last));
        return (
          <div key={i} style={{ opacity: o, transform: `translateX(${(1 - o) * 16}px)` }}>
            {parts}
          </div>
        );
      })}
    </div>
  );
};

const Frame: React.FC<{ title: string; yaml: string[]; children: React.ReactNode }> = ({ title, yaml, children }) => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: c.bg, fontFamily: sans, opacity: loopFade(f) }}>
      <AbsoluteFill
        style={{ background: `radial-gradient(700px 500px at 20% 20%, ${c.indigo}22, transparent 70%), radial-gradient(600px 500px at 85% 80%, ${c.teal}18, transparent 70%)` }}
      />
      <div style={{ position: "absolute", left: 48, top: 36, fontSize: 30, fontWeight: 700, color: c.fg }}>
        <span style={{ fontFamily: brand, color: c.violet }}>agentl</span> · {title}
      </div>
      <div
        style={{
          position: "absolute",
          left: 48,
          top: 104,
          width: 520,
          padding: "22px 24px",
          background: `${c.panel}ee`,
          border: `1px solid ${c.line}`,
          borderRadius: 14,
          overflow: "hidden",
        }}
      >
        <Yaml lines={yaml} />
      </div>
      <div style={{ position: "absolute", left: 600, top: 104, right: 48, bottom: 48 }}>{children}</div>
    </AbsoluteFill>
  );
};

const Chip: React.FC<{ text: string; color: string; style?: React.CSSProperties }> = ({ text, color, style }) => (
  <div
    style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 10,
      padding: "10px 16px",
      borderRadius: 12,
      border: `1.5px solid ${color}`,
      background: `${color}1f`,
      color: c.fg,
      fontFamily: mono,
      fontSize: 19,
      ...style,
    }}
  >
    {text}
  </div>
);

const Row: React.FC<{ cells: string[]; at: number; color?: string; strike?: boolean; tag?: string; tagColor?: string }> = ({
  cells,
  at,
  color = c.fg,
  strike,
  tag,
  tagColor,
}) => {
  const f = useCurrentFrame();
  const p = pop(f, at);
  return (
    <div
      style={{
        display: "flex",
        gap: 0,
        alignItems: "center",
        opacity: Math.min(1, p),
        transform: `translateY(${(1 - p) * 18}px)`,
        fontFamily: mono,
        fontSize: 18,
        color,
        borderBottom: `1px solid ${c.line}`,
        padding: "9px 6px",
        textDecoration: strike ? "line-through" : undefined,
      }}
    >
      {cells.map((x, i) => (
        <div key={i} style={{ flex: i === 0 ? 1.7 : 1, overflow: "hidden", whiteSpace: "nowrap", paddingRight: 12 }}>
          {x}
        </div>
      ))}
      {tag ? <div style={{ fontFamily: sans, fontSize: 15, color: tagColor ?? c.muted, marginLeft: 10 }}>{tag}</div> : null}
    </div>
  );
};

const Caption: React.FC<{ text: string; at: number; color?: string }> = ({ text, at, color = c.muted }) => {
  const f = useCurrentFrame();
  const o = lerp(f, at, at + 12, 0, 1);
  return <div style={{ opacity: o, fontSize: 21, color, marginTop: 18 }}>{text}</div>;
};

// ---------------------------------------------------------------- 1. file.read
export const ClipFiles: React.FC = () => {
  const f = useCurrentFrame();
  const files = [
    ["sales.csv", c.teal],
    ["q3.xlsx", c.green],
    ["nota.pdf", "#fb7185"],
    ["batch.zip", c.amber],
    ["events.jsonl.gz", c.blue],
  ] as const;
  return (
    <Frame title="read any file" yaml={["- id: inbox", "  use: file.read", "  with:", "    path: s3://bucket/inbox/**/*"]}>
      <div style={{ display: "flex", flexDirection: "column", gap: 26, height: "100%" }}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
          {files.map(([name, col], i) => {
            const p = pop(f, 20 + i * 8);
            const fly = lerp(f, 70 + i * 6, 95 + i * 6, 0, 1);
            return <Chip key={name} text={name} color={col} style={{ opacity: Math.min(1, p) * (1 - fly * 0.5), transform: `translateY(${fly * 12}px)` }} />;
          })}
        </div>
        <div style={{ flex: 1 }}>
          <Row cells={["_file", "id", "total"]} at={80} color={c.muted} />
          <Row cells={["sales.csv", "1", "120.00"]} at={88} />
          <Row cells={["q3.xlsx", "7", "88.50"]} at={94} />
          <Row cells={["nota.pdf", "—", "text…"]} at={100} />
          <Row cells={["batch.zip!a.csv", "3", "45.10"]} at={106} />
          <Row cells={["events.jsonl.gz", "9", "12.00"]} at={112} />
          <Caption text="Format by extension. Zip/gz expanded safely. Local, URL, S3, GCS, SFTP." at={125} />
        </div>
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 2. HTTP pagination
export const ClipHttp: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <Frame
      title="APIs with pagination"
      yaml={["- id: invoices", "  use: http.get", "  with:", "    url: ${API}/invoices", "    params: { per_page: 500 }", "    paginate: { next: next_page }", "    items: items"]}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
        {[1, 2, 3].map((p, i) => {
          const at = 30 + i * 28;
          const o = pop(f, at);
          const retry = p === 2 && f > at && f < at + 18;
          return (
            <div key={p} style={{ display: "flex", alignItems: "center", gap: 16, opacity: Math.min(1, o) }}>
              <Chip text={`GET ?page=${p}`} color={c.blue} />
              <div style={{ fontFamily: mono, fontSize: 19, color: retry ? c.amber : c.green }}>
                {retry ? "503 · retry in 1s" : `200 · 500 items${p === 3 ? " · next_page: null" : ""}`}
              </div>
            </div>
          );
        })}
        <div style={{ marginTop: 16, fontFamily: brand, fontSize: 64, color: c.fg, opacity: lerp(f, 120, 135, 0, 1) }}>
          {Math.round(lerp(f, 120, 150, 0, 1500))} records
        </div>
        <Caption text="Retries on 429/5xx with backoff. Internal addresses blocked by default." at={140} />
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 3. agent step with validation
export const ClipAgent: React.FC = () => {
  return (
    <Frame
      title="agent steps with a schema"
      yaml={["- id: classify", "  agent: Classify the expense", "    category of this invoice.", "  input: invoices", "  output:", "    category: str", "    amount: float", "    needs_review: bool"]}
    >
      <Row cells={["description", "category", "amount"]} at={20} color={c.muted} />
      <Row cells={["AWS bill Sep", "software", "312.40"]} at={40} tag="valid" tagColor={c.green} />
      <Row cells={["Uber to client", "travel", "38.90"]} at={56} tag="valid" tagColor={c.green} />
      <Row cells={["???? scan", "\"lots\"", "—"]} at={72} color={c.amber} strike tag="retry → rejected" tagColor={c.amber} />
      <Row cells={["Figma seats", "software", "90.00"]} at={88} tag="valid" tagColor={c.green} />
      <Caption text="Answers are validated against the schema. One retry, then the record is rejected, not guessed." at={110} />
      <Caption text="item.rejected → rate 0.25 → alert" at={135} color={c.amber} />
    </Frame>
  );
};

// ---------------------------------------------------------------- 4. cache
export const ClipCache: React.FC = () => {
  const f = useCurrentFrame();
  const second = f > 90;
  const calls = second ? Math.round(lerp(f, 100, 140, 0, 3)) : Math.round(lerp(f, 25, 80, 0, 1000));
  return (
    <Frame title="per-record cache" yaml={["- id: classify", "  agent: ...", "  input: invoices", "  cached: true   # default"]}>
      <div style={{ fontFamily: sans, fontSize: 26, color: c.muted }}>{second ? "run 2 · 1,003 records (3 new)" : "run 1 · 1,000 records"}</div>
      <div style={{ display: "flex", gap: 60, marginTop: 30 }}>
        <div>
          <div style={{ fontFamily: brand, fontSize: 90, color: second ? c.green : c.fg }}>{calls}</div>
          <div style={{ fontSize: 22, color: c.muted }}>LLM calls</div>
        </div>
        <div>
          <div style={{ fontFamily: brand, fontSize: 90, color: second ? c.green : c.fg }}>{second ? "$0.00" : `$${(calls * 0.0004).toFixed(2)}`}</div>
          <div style={{ fontSize: 22, color: c.muted }}>{second ? "for the 1,000 cached" : "cost"}</div>
        </div>
      </div>
      <Caption text="Re-runs and retries only pay for records that changed. SQLite locally, Postgres when shared." at={140} />
    </Frame>
  );
};

// ---------------------------------------------------------------- 5. budget
export const ClipBudget: React.FC = () => {
  const f = useCurrentFrame();
  const spent = Math.min(5, lerp(f, 20, 120, 0, 5.2));
  const hit = f > 118;
  return (
    <Frame
      title="cost budget"
      yaml={["budget:", "  max_usd: 5.00", "llm:", "  default:", "    provider: anthropic", "    price: { input: 3, output: 15 }"]}
    >
      <div style={{ fontSize: 24, color: c.muted }}>spent this run</div>
      <div style={{ fontFamily: brand, fontSize: 96, color: hit ? "#fb7185" : c.fg }}>${spent.toFixed(2)}</div>
      <div style={{ height: 22, borderRadius: 11, background: c.line, marginTop: 10, overflow: "hidden", width: 560 }}>
        <div style={{ width: `${(spent / 5) * 100}%`, height: "100%", background: hit ? "#fb7185" : `linear-gradient(90deg, ${c.teal}, ${c.amber})` }} />
      </div>
      <div style={{ marginTop: 24, opacity: lerp(f, 120, 132, 0, 1) }}>
        <Chip text="budget.exceeded · max_usd 5.0 reached" color="#fb7185" />
      </div>
      <Caption text="The run stops before the next call. Spend per step on the dashboard." at={140} />
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
    ["PagerDuty", "#fb7185"],
    ["Opsgenie", c.amber],
  ] as const;
  return (
    <Frame title="notifications" yaml={["notify:", "  rules:", "    - on: [run.failed, budget.exceeded]", "      to: [oncall, team]", "    - on: item.rejected", "      when: \"rate > 0.05\"", "      to: [mail]"]}>
      <div style={{ opacity: pop(f, 20) }}>
        <Chip text="run.failed · step load" color="#fb7185" />
      </div>
      <svg width={560} height={330} style={{ position: "absolute", top: 70, left: 0 }}>
        {channels.map(([, col], i) => {
          const y = 20 + i * 44;
          const p = lerp(f, 40 + i * 6, 70 + i * 6, 0, 1);
          return <path key={i} d={`M 60 0 C 60 ${y}, 140 ${y}, 220 ${y}`} stroke={col} strokeWidth={2.5} fill="none" pathLength={1} strokeDasharray="1 1" strokeDashoffset={1 - p} />;
        })}
      </svg>
      <div style={{ position: "absolute", top: 72, left: 230, display: "flex", flexDirection: "column", gap: 6 }}>
        {channels.map(([name, col], i) => (
          <div key={name} style={{ opacity: Math.min(1, pop(f, 60 + i * 6)) }}>
            <Chip text={name} color={col} style={{ padding: "6px 14px", fontSize: 17 }} />
          </div>
        ))}
      </div>
      <div style={{ position: "absolute", bottom: 0 }}>
        <Caption text="Deduplicated per pipeline and step. A failing channel never fails the run." at={130} />
      </div>
    </Frame>
  );
};

// ---------------------------------------------------------------- 7. upsert
export const ClipUpsert: React.FC = () => {
  const f = useCurrentFrame();
  const run2 = f > 90;
  return (
    <Frame title="idempotent loads" yaml={["- id: save", "  use: postgres.write", "  with:", "    dsn: ${DATABASE_URL}", "    table: invoices", "    mode: upsert", "    key: number"]}>
      <div style={{ fontSize: 24, color: c.muted }}>{run2 ? "run 2: same file again, one total changed" : "run 1"}</div>
      <div style={{ marginTop: 14 }}>
        <Row cells={["number", "total", "updated"]} at={10} color={c.muted} />
        <Row cells={["NF-1", "120.00", "run 1"]} at={25} />
        <Row cells={["NF-2", run2 ? "99.90" : "80.00", run2 ? "run 2" : "run 1"]} at={35} color={run2 ? c.green : c.fg} />
        <Row cells={["NF-3", "45.10", "run 1"]} at={45} />
      </div>
      <Caption text="3 rows after 2 runs: no duplicates. Postgres, MySQL, SQL Server, Snowflake, BigQuery…" at={120} />
    </Frame>
  );
};

// ---------------------------------------------------------------- 8. dashboard / observability
export const ClipObserve: React.FC = () => {
  const f = useCurrentFrame();
  const steps = [
    ["extract", c.blue],
    ["clean", c.teal],
    ["classify", c.violet],
    ["save", c.green],
  ] as const;
  return (
    <Frame title="dashboard, logs and traces" yaml={["$ agentl run p.yaml --otel \\", "    --log-format json", "$ agentl dashboard"]}>
      <div style={{ display: "flex", gap: 22, alignItems: "center" }}>
        {steps.map(([name, col], i) => {
          const on = f > 20 + i * 22;
          return (
            <React.Fragment key={name}>
              <div
                style={{
                  padding: "16px 18px",
                  borderRadius: name === "classify" ? 30 : 12,
                  border: `2px solid ${on ? col : c.line}`,
                  boxShadow: on ? `0 0 26px ${col}88` : "none",
                  color: c.fg,
                  fontSize: 21,
                  fontWeight: 700,
                }}
              >
                {name}
              </div>
              {i < steps.length - 1 ? <div style={{ width: 16, height: 2, background: on ? col : c.line }} /> : null}
            </React.Fragment>
          );
        })}
      </div>
      <div style={{ marginTop: 34, fontFamily: mono, fontSize: 13, lineHeight: "24px", color: c.muted }}>
        {[
          '{"level":"info","type":"step.succeeded","step":"extract","data":{"records":1240}}',
          '{"level":"warning","type":"item.rejected","step":"classify","data":{"count":9}}',
          '{"level":"info","type":"step.succeeded","step":"classify","data":{"cost_usd":0.41}}',
          '{"level":"info","type":"run.succeeded","data":{"seconds":38.2}}',
        ].map((l, i) => (
          <div key={i} style={{ opacity: lerp(f, 40 + i * 22, 50 + i * 22, 0, 1), whiteSpace: "nowrap", overflow: "hidden", color: l.includes("warning") ? c.amber : c.muted }}>
            {l}
          </div>
        ))}
      </div>
      <Caption text="A trace per run, a span per step, metrics, Sentry for failures." at={135} />
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
