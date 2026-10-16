"""Generate public/sfx/music.mp3: a soft lo-fi loop (warm chord pad, gentle kick and hat, 88 bpm,
32 s), synthesized here, so no third-party audio and no licence to track.
Run: python3 scripts/music.py (writes the .wav, then encodes the .mp3 with ffmpeg)"""

import subprocess
import wave
from pathlib import Path

import numpy as np

RATE = 44100
BPM = 88
BEAT = 60 / BPM
SECONDS = 32
OUT = Path(__file__).parent.parent / "public" / "sfx" / "music.wav"

n = int(RATE * SECONDS)
t = np.arange(n) / RATE
mix = np.zeros((n, 2))
rng = np.random.default_rng(3)


def note(freq: float) -> float:
    return 440 * 2 ** ((freq - 69) / 12)


def lowpass(x: np.ndarray, a: float) -> np.ndarray:
    y = np.zeros_like(x)
    for i in range(1, len(x)):
        y[i] = y[i - 1] + a * (x[i] - y[i - 1])
    return y


# Chords, one bar (4 beats) each: Fmaj7, Em7, Dm7, Cmaj9 (MIDI notes).
CHORDS = [[53, 57, 60, 64], [52, 55, 59, 62], [50, 53, 57, 60], [48, 55, 62, 64]]
bar = 4 * BEAT
for b in range(int(SECONDS / bar) + 1):
    chord = CHORDS[b % len(CHORDS)]
    start = int(b * bar * RATE)
    length = min(int(bar * RATE * 1.15), n - start)  # small overlap between bars
    if length <= 0:
        break
    tt = np.arange(length) / RATE
    shape = np.minimum(1, tt / 0.6) * np.minimum(1, (length / RATE - tt) / 0.5)
    for i, m in enumerate(chord):
        f = note(m)
        detune = 1 + 0.002 * (i - 1.5)
        tone = np.sin(2 * np.pi * f * detune * tt) + 0.3 * np.sin(2 * np.pi * 2 * f * tt + 0.5)
        tone *= 1 + 0.15 * np.sin(2 * np.pi * 0.3 * tt + i)  # slow movement
        pan = 0.3 + 0.4 * (i / 3)
        mix[start : start + length, 0] += tone * shape * (1 - pan) * 0.18
        mix[start : start + length, 1] += tone * shape * pan * 0.18
    bass = np.sin(2 * np.pi * note(chord[0] - 12) * tt) * shape * 0.35
    mix[start : start + length] += bass[:, None]

# Soft kick on beats 1 and 3, hat on the off-beats.
for k in range(int(SECONDS / BEAT)):
    s = int(k * BEAT * RATE)
    if k % 2 == 0:
        m = min(int(0.25 * RATE), n - s)
        tt = np.arange(m) / RATE
        kick = np.sin(2 * np.pi * (45 + 60 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 12) * 0.5
        mix[s : s + m] += kick[:, None]
    h = int((k + 0.5) * BEAT * RATE)
    m = min(int(0.05 * RATE), n - h)
    if m > 0:
        hat = np.diff(rng.normal(size=m), prepend=0) * np.exp(-np.arange(m) / RATE * 90) * 0.05
        mix[h : h + m] += hat[:, None]

# Warmth: gentle low-pass, then fade in and out.
mix = np.stack([lowpass(mix[:, ch], 0.25) for ch in (0, 1)], axis=1)
fade = np.minimum(1, t / 1.5) * np.minimum(1, (SECONDS - t) / 2)
mix *= fade[:, None]
mix = mix / np.abs(mix).max() * 0.8

OUT.parent.mkdir(parents=True, exist_ok=True)
with wave.open(str(OUT), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes((mix * 32767).astype("<i2").tobytes())

subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(OUT),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(OUT.with_suffix(".mp3")),
    ],
    check=True,
)
OUT.unlink()
