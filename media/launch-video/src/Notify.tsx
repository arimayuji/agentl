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

// Notifications: the notify block, a run that fails, and the alert fanning out to a phone where
// every service's message lands with a chime. Services are shown as named color chips, not their
// logos (trademarks). Carioca palette (media/brand). The run output is sample output.
export const NOTIFY_FPS = 30;

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

const T = {
  yaml: 6,
  run: 96,
  fail: 150,
  fan: 176,
  outro: 372,
};
export const NOTIFY_FRAMES = 450;

const clamp = { extrapolateLeft: "clamp", extrapolateRight: "clamp" } as const;
const ease = Easing.bezier(0.16, 1, 0.3, 1);
const lerp = (f: number, a: number, b: number, from: number, to: number) =>
  interpolate(f, [a, b], [from, to], { ...clamp, easing: ease });

// Each service: chip color and the message as it shows on the phone.
const SERVICES = [
  { name: "WhatsApp", color: "#1f9d55", text: "invoices failed at classify: HTTP 503" },
  { name: "Slack", color: "#4a154b", text: "[agentl] invoices: run.failed step classify" },
  { name: "Telegram", color: "#229ed9", text: "invoices failed, 12 records rejected" },
  { name: "SMS", color: k.ink, text: "agentl: invoices failed (classify)" },
  { name: "Discord", color: "#5865f2", text: "run.failed in invoices / classify" },
  { name: "Teams", color: "#5b5fc7", text: "agentl invoices: run.failed (classify)" },
  { name: "ntfy", color: "#338574", text: "invoices: run.failed" },
  { name: "Email", color: k.sunset, text: "Subject: agentl invoices: run.failed" },
];
const ARRIVE = (i: number) => T.fan + 14 + i * 20;

const Backdrop: React.FC = () => {
  const f = useCurrentFrame();
  const t = f / NOTIFY_FPS;
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

const Card: React.FC<{ title: string; children: React.ReactNode; width: number; at: number; tint?: string }> = ({
  title,
  children,
  width,
  at,
  tint,
}) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({ frame: f - at, fps, config: { damping: 14, stiffness: 120 } });
  return (
    <div style={{
      width, borderRadius: 22, background: k.white, border: `2px solid ${tint ?? k.line}`,
      boxShadow: tint ? `0 0 0 6px ${tint}22, 0 24px 60px rgba(11,27,43,.14)` : "0 24px 60px rgba(11,27,43,.12)",
      overflow: "hidden", opacity: Math.min(1, s * 1.4),
      transform: `translateY(${(1 - s) * 60}px)`, filter: s < 0.98 ? `blur(${(1 - s) * 10}px)` : undefined,
    }}>
      <div style={{ display: "flex", gap: 10, padding: "14px 20px", borderBottom: `1px solid ${k.line}`, alignItems: "center" }}>
        {["#ff5f56", "#ffbd2e", "#27c93f"].map((col) => <div key={col} style={{ width: 13, height: 13, borderRadius: 7, background: col }} />)}
        <div style={{ marginLeft: 12, color: k.muted, fontFamily: mono, fontSize: 21 }}>{title}</div>
      </div>
      <div style={{ padding: "18px 24px", fontFamily: mono }}>{children}</div>
    </div>
  );
};

const YAML: Array<[string, string]> = [
  ["notify:", k.blue],
  ["  channels:", k.blue],
  ["    phone: { type: whatsapp, to: \"55119...\" }", k.ink],
  ["    team:  { type: slack, webhook: ${SLACK} }", k.ink],
  ["    oncall: { type: sms, to: \"+55119...\" }", k.ink],
  ["  rules:", k.blue],
  ["    - { on: run.failed, to: [phone, team, oncall] }", k.sunset],
];

const Left: React.FC<{ width: number; size: number }> = ({ width, size }) => {
  const f = useCurrentFrame();
  const shake = f >= T.fail && f < T.fail + 14 ? Math.sin((f - T.fail) * 2.2) * (14 - (f - T.fail)) * 1.2 : 0;
  const failed = f >= T.fail;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 26 }}>
      <Card title="pipeline.yaml" width={width} at={T.yaml}>
        {YAML.map(([text, color], i) => (
          <div key={i} style={{ fontSize: size, lineHeight: 1.55, whiteSpace: "pre", color,
            opacity: lerp(f, T.yaml + 8 + i * 8, T.yaml + 20 + i * 8, 0, 1),
            transform: `translateX(${lerp(f, T.yaml + 8 + i * 8, T.yaml + 20 + i * 8, -24, 0)}px)` }}>
            {text}
          </div>
        ))}
      </Card>
      <div style={{ transform: `translateX(${shake}px)` }}>
        <Card title="terminal" width={width} at={T.run} tint={failed ? k.red : undefined}>
          <div style={{ fontSize: size + 2, fontWeight: 700, color: k.ink }}>
            <span style={{ color: k.blue }}>$ </span>agentl run pipeline.yaml
          </div>
          {[
            ["read      40 records", k.ink, T.run + 18],
            ["classify  HTTP 503 from the LLM provider", k.red, T.fail],
            ["run.failed  notifying phone, team, oncall", k.red, T.fail + 10],
          ].map(([text, color, at]) => (
            <div key={text as string} style={{ fontSize: size, lineHeight: 1.6, whiteSpace: "pre", color: color as string,
              opacity: lerp(f, at as number, (at as number) + 8, 0, 1) }}>
              {text as string}
            </div>
          ))}
        </Card>
      </div>
    </div>
  );
};

const Phone: React.FC<{ scale: number }> = ({ scale }) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  const inS = spring({ frame: f - (T.fan - 10), fps, config: { damping: 14, stiffness: 110 } });
  const arrived = SERVICES.filter((_, i) => f >= ARRIVE(i)).length;
  const buzz = SERVICES.some((_, i) => f >= ARRIVE(i) && f < ARRIVE(i) + 6) ? Math.sin(f * 3) * 3 : 0;
  const W = 470;
  const H = 940;
  return (
    <div style={{ transform: `scale(${scale * (0.85 + inS * 0.15)}) translateY(${(1 - inS) * 200}px) rotate(${buzz * 0.3}deg)`,
      opacity: Math.min(1, inS * 1.5), transformOrigin: "center" }}>
      <div style={{ width: W, height: H, borderRadius: 64, background: k.ink, padding: 16,
        boxShadow: "0 50px 120px rgba(11,27,43,.28)" }}>
        <div style={{ position: "relative", width: "100%", height: "100%", borderRadius: 50, overflow: "hidden",
          background: "linear-gradient(160deg, #9fd0f5, #fff1a8 55%, #ffc2a8)" }}>
          <div style={{ position: "absolute", top: 14, left: "50%", width: 120, height: 32, marginLeft: -60, borderRadius: 16, background: k.ink }} />
          <div style={{ position: "absolute", top: 70, width: "100%", textAlign: "center", fontFamily: sans,
            fontSize: 86, fontWeight: 300, color: k.ink }}>09:41</div>
          <div style={{ position: "absolute", top: 176, width: "100%", textAlign: "center", fontFamily: sans,
            fontSize: 24, color: k.muted }}>{arrived > 0 ? `${arrived} new` : "Thursday"}</div>
          {SERVICES.map((svc, i) => {
            const local = f - ARRIVE(i);
            if (local < 0) return null;
            const s = spring({ frame: local, fps, config: { damping: 13, stiffness: 170 } });
            // Newest on top: each later arrival pushes this one down one slot.
            const slot = SERVICES.filter((_, j) => j > i && f >= ARRIVE(j)).length;
            const y = 230 + slot * 112 + (1 - s) * -80;
            const fade = interpolate(slot, [4, 6], [1, 0], clamp);
            return (
              <div key={svc.name} style={{ position: "absolute", left: 18, right: 18, top: y, height: 98, borderRadius: 24,
                background: "rgba(255,255,255,.92)", boxShadow: "0 8px 24px rgba(11,27,43,.12)", padding: "14px 18px",
                display: "flex", gap: 14, alignItems: "center", opacity: Math.min(1, s * 1.5) * fade,
                transform: `scale(${0.9 + s * 0.1})`, fontFamily: sans }}>
                <div style={{ width: 56, height: 56, borderRadius: 16, background: svc.color, flexShrink: 0, color: k.white,
                  display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 26 }}>
                  {svc.name[0]}
                </div>
                <div style={{ overflow: "hidden" }}>
                  <div style={{ fontSize: 22, fontWeight: 700, color: k.ink }}>{svc.name}</div>
                  <div style={{ fontSize: 19, color: k.muted, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {svc.text}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

const Outro: React.FC = () => {
  const f = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const portrait = height > width;
  const s = (d: number) => spring({ frame: f - T.outro - d, fps, config: { damping: 14, stiffness: 130 } });
  const chips = ["WhatsApp", "SMS", "Slack", "Teams", "Telegram", "Discord", "Google Chat", "Mattermost",
    "ntfy", "Pushover", "Matrix", "Email", "PagerDuty", "Opsgenie", "Webhook"];
  return (
    <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 34,
      fontFamily: sans, opacity: Math.min(1, s(0) * 1.5), padding: 60 }}>
      <div style={{ fontSize: portrait ? 78 : 84, fontWeight: 800, letterSpacing: -2, color: k.ink, textAlign: "center",
        lineHeight: 1.1, transform: `translateY(${(1 - s(0)) * 40}px)` }}>
        A run fails anywhere.<br /><span style={{ color: k.blue }}>You hear about it everywhere.</span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14, justifyContent: "center", maxWidth: portrait ? 900 : 1300 }}>
        {chips.map((c, i) => (
          <div key={c} style={{ padding: "10px 22px", borderRadius: 999, background: k.white, border: `1px solid ${k.line}`,
            fontSize: 28, color: k.ink, opacity: Math.min(1, s(8 + i * 2) * 1.5), transform: `scale(${0.8 + s(8 + i * 2) * 0.2})` }}>
            {c}
          </div>
        ))}
      </div>
      <div style={{ fontFamily: mono, fontSize: 30, color: k.muted, opacity: s(22) }}>
        notify: for run events · whatsapp.send, slack.send, ... for records
      </div>
      <Img src={staticFile("agentl-wordmark-light.svg")} style={{ width: portrait ? 420 : 460, opacity: s(30), marginTop: 10 }} />
    </AbsoluteFill>
  );
};

const Sound: React.FC = () => (
  <>
    <Audio src={staticFile("sfx/music.mp3")} volume={(f: number) =>
      0.12 * interpolate(f, [0, 20, NOTIFY_FRAMES - 40, NOTIFY_FRAMES], [0, 1, 1, 0], clamp)} />
    <Sequence from={T.yaml} durationInFrames={20} layout="none"><Audio src={staticFile("sfx/whoosh-in.wav")} volume={0.4} /></Sequence>
    <Sequence from={T.run} durationInFrames={20} layout="none"><Audio src={staticFile("sfx/enter.wav")} volume={0.6} /></Sequence>
    <Sequence from={T.fail} durationInFrames={20} layout="none"><Audio src={staticFile("sfx/alert.wav")} volume={0.6} /></Sequence>
    {SERVICES.map((_, i) => (
      <Sequence key={i} from={ARRIVE(i)} durationInFrames={24} layout="none">
        <Audio src={staticFile(`sfx/ding${i % 3}.wav`)} volume={0.42} />
      </Sequence>
    ))}
    <Sequence from={T.outro} durationInFrames={20} layout="none"><Audio src={staticFile("sfx/whoosh-in.wav")} volume={0.4} /></Sequence>
  </>
);

export const Notify: React.FC = () => {
  const f = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const portrait = height > width;
  const sceneOut = interpolate(f, [T.outro - 16, T.outro], [1, 0], clamp);
  const zoom = interpolate(f, [T.outro - 16, T.outro], [1, 1.12], clamp);
  return (
    <AbsoluteFill>
      <Backdrop />
      <Sound />
      <AbsoluteFill style={{ opacity: sceneOut, transform: `scale(${zoom})`, filter: sceneOut < 0.98 ? `blur(${(1 - sceneOut) * 14}px)` : undefined,
        alignItems: "center", justifyContent: "center", flexDirection: portrait ? "column" : "row", gap: portrait ? 30 : 90 }}>
        <Left width={portrait ? 940 : 920} size={portrait ? 24 : 25} />
        <Phone scale={portrait ? 0.82 : 0.95} />
      </AbsoluteFill>
      {f >= T.outro && <Outro />}
      <div style={{ position: "absolute", bottom: 24, width: "100%", textAlign: "center", fontFamily: sans, fontSize: 18,
        color: k.muted, opacity: sceneOut }}>sample output</div>
    </AbsoluteFill>
  );
};
