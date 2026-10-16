"""Generate public/sfx/music.mp3: a soft lo-fi loop, synthesized here, so no third-party audio and
no licence to track. Electric piano (FM, Rhodes-like) on jazz chords, warm bass, swung brushed
drums, vinyl crackle and a little tape wobble and saturation. 78 bpm, 32 s.
Run: python3 scripts/music.py (writes the .wav, then encodes the .mp3 with ffmpeg)"""

import subprocess
import wave
from pathlib import Path

import numpy as np

RATE = 44100
BPM = 78
BEAT = 60 / BPM
SECONDS = 32
OUT = Path(__file__).parent.parent / "public" / "sfx" / "music.wav"

n = int(RATE * SECONDS)
t = np.arange(n) / RATE
left = np.zeros(n)
right = np.zeros(n)
rng = np.random.default_rng(11)


def hz(midi: float) -> float:
    return 440 * 2 ** ((midi - 69) / 12)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """One-pole low-pass at `cutoff` Hz."""
    a = 1 - np.exp(-2 * np.pi * cutoff / RATE)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += a * (v - acc)
        y[i] = acc
    return y


def add(buf_l: np.ndarray, buf_r: np.ndarray, start: float, sig: np.ndarray, pan: float, gain: float) -> None:
    s = int(start * RATE)
    if s >= n:
        return
    sig = sig[: n - s]
    buf_l[s : s + len(sig)] += sig * gain * np.cos(pan * np.pi / 2)
    buf_r[s : s + len(sig)] += sig * gain * np.sin(pan * np.pi / 2)


def epiano(freq: float, dur: float, vel: float) -> np.ndarray:
    """Two-operator FM tine with a bell attack that softens, like a Rhodes."""
    m = int(RATE * (dur + 1.2))
    tt = np.arange(m) / RATE
    index = 1.6 * np.exp(-tt * 5) + 0.25
    mod = np.sin(2 * np.pi * freq * tt) * index
    tone = np.sin(2 * np.pi * freq * tt + mod) + 0.15 * np.sin(2 * np.pi * freq * 2 * tt)
    release = np.clip((dur + 1.2 - tt) / 1.2, 0, 1)
    env = np.minimum(1, tt / 0.004) * np.exp(-tt * 1.1) * release
    return tone * env * vel


def horn(freq: float, dur: float, vel: float) -> np.ndarray:
    """A soft muted horn, between a sax and a cup-muted trumpet: odd and even harmonics with a
    gentle formant, a slow swell, a breath of air on the attack and vibrato that grows in."""
    m = int(RATE * (dur + 0.35))
    tt = np.arange(m) / RATE
    vib = 1 + 0.006 * np.sin(2 * np.pi * 5.2 * tt) * np.clip((tt - 0.18) / 0.3, 0, 1)
    phase = 2 * np.pi * freq * np.cumsum(vib) / RATE
    tone = sum(
        a * np.sin(h * phase) for h, a in ((1, 1.0), (2, 0.55), (3, 0.38), (4, 0.18), (5, 0.12), (6, 0.05))
    )
    tone = lowpass(tone, 1900)  # muted, round
    air = lowpass(rng.normal(size=m), 3200) * np.exp(-tt * 9) * 0.25
    swell = np.clip(tt / 0.09, 0, 1) * np.clip((dur + 0.35 - tt) / 0.35, 0, 1)
    return (tone + air) * swell * vel


# Horn melody over the four chords (MIDI, beat offset, beats): lazy, behind the beat, lots of space.
MELODY = [
    [(69, 0.5, 1.5), (72, 2.25, 0.75), (69, 3.0, 1.0)],
    [(67, 0.75, 1.25), (65, 2.0, 0.5), (64, 2.5, 1.5)],
    [(64, 1.0, 1.0), (67, 2.0, 0.75), (71, 2.75, 1.25)],
    [(70, 0.5, 1.0), (69, 1.5, 0.5), (67, 2.0, 0.5), (65, 2.5, 1.5)],
]

# Chords (MIDI, voiced close): Dm9, G13, Cmaj9, A7(b13). One bar each.
CHORDS = [
    [50, 53, 57, 60, 64],
    [43, 53, 57, 59, 64],
    [48, 52, 55, 59, 62],
    [45, 55, 58, 61, 65],
]
BAR = 4 * BEAT
SWING = BEAT * 0.17  # late off-beats

for b in range(int(SECONDS / BAR) + 1):
    chord = CHORDS[b % len(CHORDS)]
    t0 = b * BAR
    # Piano: chord on beat 1 (slightly rolled), a softer stab on the "and" of 2.
    for i, note in enumerate(chord[1:]):
        add(left, right, t0 + i * 0.012, epiano(hz(note), BAR * 0.9, 0.34), 0.35 + 0.08 * i, 1)
        add(
            left, right, t0 + 1.5 * BEAT + SWING, epiano(hz(note + 12 if i == 3 else note), 0.4, 0.12), 0.6, 1
        )
    # Horn: the melody on every bar after the first two, an octave lower every other pass.
    if b >= 2:
        shift = 0 if (b // 4) % 2 == 0 else -12
        for note, at, beats in MELODY[b % len(MELODY)]:
            add(left, right, t0 + at * BEAT + 0.03, horn(hz(note + shift), beats * BEAT, 0.16), 0.58, 1)
    # Bass: root on 1, fifth on 3, round and short.
    for when, note in ((0, chord[0] - 12), (2 * BEAT, chord[0] - 5)):
        m = int(RATE * 0.9)
        tt = np.arange(m) / RATE
        bass = np.sin(2 * np.pi * hz(note) * tt) + 0.2 * np.sin(2 * np.pi * hz(note) * 2 * tt)
        bass *= np.minimum(1, tt / 0.01) * np.exp(-tt * 3)
        add(left, right, t0 + when, bass, 0.5, 0.24)

# Drums: soft kick on 1 and the "and" of 3, rim on 2 and 4, brushed hats on swung eighths.
for beat in range(int(SECONDS / BEAT)):
    bt = beat * BEAT
    pos = beat % 4
    if pos == 0 or pos == 2:
        m = int(RATE * 0.3)
        tt = np.arange(m) / RATE
        kick = np.sin(2 * np.pi * (48 + 70 * np.exp(-tt * 35)) * tt) * np.exp(-tt * 9)
        add(left, right, bt + (BEAT / 2 + SWING if pos == 2 else 0), kick, 0.5, 0.18)
    if pos in (1, 3):
        m = int(RATE * 0.12)
        tt = np.arange(m) / RATE
        rim = lowpass(rng.normal(size=m), 2500) * np.exp(-tt * 40) + 0.4 * np.sin(
            2 * np.pi * 820 * tt
        ) * np.exp(-tt * 60)
        add(left, right, bt, rim, 0.45, 0.07)
    for half in (0, 1):
        m = int(RATE * 0.06)
        tt = np.arange(m) / RATE
        hat = np.diff(rng.normal(size=m), prepend=0) * np.exp(-tt * 70)
        add(left, right, bt + half * (BEAT / 2 + SWING), hat, 0.62, 0.018 if half else 0.026)

# Tape: slow wow (pitch wobble by resampling), warmth (low-pass) and soft saturation.
wobble = 1 + 0.0018 * np.sin(2 * np.pi * 0.45 * t)
idx = np.clip(np.cumsum(wobble) - 1, 0, n - 1)
left = np.interp(idx, np.arange(n), left)
right = np.interp(idx, np.arange(n), right)
left, right = lowpass(left, 5200), lowpass(right, 5200)

# Vinyl: low hiss plus sparse crackles.
hiss = lowpass(rng.normal(size=n), 3000) * 0.0025
crackle = np.zeros(n)
pops = rng.integers(0, n, size=int(SECONDS * 9))
crackle[pops] = rng.normal(size=len(pops)) * 0.08
crackle = lowpass(crackle, 4000)
left += hiss + crackle
right += hiss + np.roll(crackle, 37)

mix = np.stack([left, right], axis=1)
mix = np.tanh(mix * 1.6) / np.tanh(1.6)
fade = np.minimum(1, t / 2.0) * np.minimum(1, (SECONDS - t) / 2.5)
mix *= fade[:, None]
mix = mix / np.abs(mix).max() * 0.85

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
        "160k",
        str(OUT.with_suffix(".mp3")),
    ],
    check=True,
)
OUT.unlink()
