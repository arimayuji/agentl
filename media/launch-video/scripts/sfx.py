"""Generate the typing sound effects in public/sfx: synthesized here, so no third-party audio
and no licence to track. Run: python3 scripts/sfx.py"""

import wave
from pathlib import Path

import numpy as np

RATE = 44100
OUT = Path(__file__).parent.parent / "public" / "sfx"


def save(name: str, x: np.ndarray) -> None:
    x = x / (np.abs(x).max() or 1) * 0.9
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / RATE
    return np.minimum(1, t / attack) * np.exp(-t / decay)


def key(seed: int, pitch: float = 1.0, body: float = 1.0) -> np.ndarray:
    """A keystroke: a bright click on top of a short, damped low thump."""
    rng = np.random.default_rng(seed)
    n = int(RATE * 0.09)
    t = np.arange(n) / RATE
    click = rng.normal(size=n) * env(n, 0.0004, 0.004)
    click = np.diff(click, prepend=0)  # high-pass: crisper
    thump = np.sin(2 * np.pi * (180 * pitch) * t) * env(n, 0.001, 0.018) * 0.6 * body
    rattle = rng.normal(size=n) * env(n, 0.002, 0.012) * 0.15
    return click + thump + rattle


def whoosh(rise: bool) -> np.ndarray:
    rng = np.random.default_rng(7)
    n = int(RATE * 0.6)
    x = rng.normal(size=n)
    # Swept one-pole low-pass on noise.
    cut = np.linspace(0.02, 0.35, n) if rise else np.linspace(0.35, 0.02, n)
    y = np.zeros(n)
    for i in range(1, n):
        y[i] = y[i - 1] + cut[i] * (x[i] - y[i - 1])
    shape = np.sin(np.linspace(0, np.pi, n)) ** 2
    return y * shape


def tick() -> np.ndarray:
    n = int(RATE * 0.05)
    t = np.arange(n) / RATE
    return np.sin(2 * np.pi * 1400 * t) * env(n, 0.0005, 0.008)


def ding(freq: float) -> np.ndarray:
    """A notification chime: two soft bell partials."""
    n = int(RATE * 0.7)
    t = np.arange(n) / RATE
    bell = np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(2 * np.pi * freq * 2.76 * t) * np.exp(-t * 6)
    return bell * env(n, 0.003, 0.22)


def alert() -> np.ndarray:
    """A low two-note buzz for a failed run."""
    n = int(RATE * 0.5)
    t = np.arange(n) / RATE
    tone = np.where(t < 0.18, np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 175 * t))
    return np.tanh(tone * 2.5) * env(n, 0.005, 0.2)


OUT.mkdir(parents=True, exist_ok=True)
for i in range(6):
    save(f"key{i}.wav", key(i, pitch=0.9 + 0.05 * i))
save("enter.wav", key(42, pitch=0.6, body=1.8))
save("tick.wav", tick())
save("whoosh-in.wav", whoosh(True))
save("whoosh-out.wav", whoosh(False))
for i, f in enumerate((988, 1175, 1319)):  # B5, D6, E6: varied so a burst of alerts is not one note
    save(f"ding{i}.wav", ding(f))
save("alert.wav", alert())
