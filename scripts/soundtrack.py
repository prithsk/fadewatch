"""Synthesise the demo soundtrack from the tour's cue list (numpy + scipy only).

    python scripts/soundtrack.py cues.json out.wav --seconds 105

A dark-synth bed in A minor whose intensity follows the story, plus HUD sound
effects placed on the cues the page recorded while it was captured
(caption, lock, alert, drop, mode, plans, score).
"""
from __future__ import annotations

import argparse
import json
import wave

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000
BPM = 96
BEAT = 60 / BPM
rng = np.random.default_rng(7)


def midi(n: float) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def saw(f, t):
    return 2 * ((f * t) % 1.0) - 1


def tri(f, t):
    return 2 * np.abs(2 * ((f * t) % 1.0) - 1) - 1


def adsr(n, a, r):
    e = np.ones(n)
    na, nr = max(1, int(a * SR)), max(1, int(r * SR))
    e[:na] = np.linspace(0, 1, na)
    if nr < n:
        e[-nr:] *= np.linspace(1, 0, nr)
    return e


def lp(x, fc, order=2):
    return sosfilt(butter(order, fc, "low", fs=SR, output="sos"), x)


def hp(x, fc, order=2):
    return sosfilt(butter(order, fc, "high", fs=SR, output="sos"), x)


def place(buf, x, t, gain=1.0, pan=0.0):
    i = int(t * SR)
    if i >= buf.shape[1]:
        return
    n = min(len(x), buf.shape[1] - i)
    l, r = np.sqrt(0.5 * (1 - pan)), np.sqrt(0.5 * (1 + pan))
    buf[0, i:i + n] += x[:n] * gain * l
    buf[1, i:i + n] += x[:n] * gain * r


def intensity(t):
    """Story energy: intro, Phoenix, storm, failing LNA, interference, score, outro."""
    pts = [(0, 0.15), (12, 0.3), (14, 0.5), (30, 0.55), (35, 0.85), (60, 0.9), (63, 0.6), (78, 0.7),
           (87, 0.75), (89, 1.0), (98, 0.6), (104, 0.25), (108, 0.0)]
    xs, ys = zip(*pts)
    return np.interp(t, xs, ys)


def music(seconds):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    buf = np.zeros((2, n))
    prog = [(57, [0, 3, 7]), (53, [0, 4, 7]), (48, [0, 4, 7]), (55, [0, 4, 7])]  # Am F C G
    bar = 4 * BEAT
    seg = 2 * bar
    # pad: detuned saws, one chord every two bars, slow filter breathing
    for k in range(int(seconds / seg) + 1):
        root, ch = prog[k % 4]
        t0 = k * seg
        m = int((seg + 1.2) * SR)
        tt = np.arange(m) / SR
        v = np.zeros(m)
        for iv in ch:
            for det in (-0.08, 0.0, 0.08):
                v += saw(midi(root + 12 + iv + det), tt)
        v = lp(v, 900 + 500 * np.sin(k)) * adsr(m, 0.9, 1.4) * 0.06
        place(buf, v, t0, pan=-0.25)
        place(buf, np.roll(v, 600), t0, pan=0.25)
        # bass pulse: eighth notes on the root
        for e in range(int(seg / (BEAT / 2))):
            te = t0 + e * BEAT / 2
            ln = int(0.22 * SR)
            tb = np.arange(ln) / SR
            b = (np.sin(2 * np.pi * midi(root - 12) * tb) + 0.3 * saw(midi(root - 12), tb)) * np.exp(-tb / 0.12)
            place(buf, lp(b, 600) * 0.2 * float(intensity(te) > 0.25), te)
    # kick, hats, arp
    nbeats = int(seconds / BEAT)
    for b in range(nbeats):
        tb0 = b * BEAT
        it = float(intensity(tb0))
        if it > 0.45 and b % 2 == 0:
            ln = int(0.4 * SR); tk = np.arange(ln) / SR
            f = 45 + 80 * np.exp(-tk / 0.03)
            kick = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tk / 0.16)
            place(buf, kick, tb0, gain=0.55 * it)
        for h in range(2):
            th = tb0 + h * BEAT / 2
            if it > 0.4:
                ln = int(0.05 * SR)
                hat = hp(rng.standard_normal(ln), 7000) * np.exp(-np.arange(ln) / SR / 0.012)
                place(buf, hat, th, gain=(0.05 if h else 0.03) * it, pan=0.35)
        root, ch = prog[int(tb0 / seg) % 4]
        if it > 0.7:
            for q in range(4):
                tq = tb0 + q * BEAT / 4
                note = root + 24 + ch[(b * 4 + q) % 3] + (12 if (b + q) % 7 == 0 else 0)
                ln = int(0.16 * SR); tn = np.arange(ln) / SR
                pl = tri(midi(note), tn) * np.exp(-tn / 0.06)
                place(buf, lp(pl, 4200), tq, gain=0.07 * it, pan=(-0.5 if q % 2 else 0.5))
    fade = np.clip((seconds - t) / 3.0, 0, 1) * np.clip(t / 1.5, 0, 1)
    return buf * fade


def sfx(kind, extra):
    def beep(f, d, shape=np.sin, decay=0.04):
        tt = np.arange(int(d * SR)) / SR
        return shape(2 * np.pi * f * tt) * np.exp(-tt / decay) * adsr(len(tt), 0.003, 0.01)

    if kind == "caption":
        ln = int(0.38 * SR); tt = np.arange(ln) / SR
        noise = rng.standard_normal(ln)
        sweep = np.concatenate([lp(noise[i:i + 1200], 400 + 9000 * (i / ln)) for i in range(0, ln, 1200)])[:ln]
        w = sweep * np.sin(np.pi * tt / tt[-1]) ** 2 * 0.22
        w[: len(beep(2400, 0.03))] += beep(2400, 0.03) * 0.12
        return w
    if kind == "lock":
        out = np.zeros(int(0.32 * SR))
        for j, f in enumerate((1400, 1900, 2600)):
            b = beep(f, 0.05, decay=0.03) * (0.22 if j < 2 else 0.12)
            out[int(j * 0.075 * SR):int(j * 0.075 * SR) + len(b)] += b
        return out
    if kind == "alert":
        if extra.get("sev") == "critical":
            out = np.zeros(int(0.3 * SR))
            for j in range(2):
                b = beep(700, 0.09, decay=0.06) * 0.2
                out[int(j * 0.13 * SR):int(j * 0.13 * SR) + len(b)] += b
            return out
        return beep(1050, 0.08, decay=0.05) * 0.14
    if kind == "drop":
        ln = int(0.8 * SR); tt = np.arange(ln) / SR
        f = 1100 * np.exp(-tt / 0.22) + 160
        s = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt / 0.35) * 0.35
        s += lp(rng.standard_normal(ln), 2500) * np.exp(-tt / 0.08) * 0.15
        return s
    if kind == "mode":
        ln = int(0.3 * SR); tt = np.arange(ln) / SR
        return (np.sin(2 * np.pi * 60 * tt) * 0.15 + hp(rng.standard_normal(ln), 3000) * 0.08) * np.exp(-tt / 0.08)
    if kind == "plans":
        out = np.zeros(int(0.9 * SR))
        for j, nn in enumerate((69, 73, 76, 81)):
            b = beep(midi(nn), 0.5, shape=lambda x: np.sin(x) + 0.3 * np.sin(2 * x), decay=0.25) * 0.14
            out[int(j * 0.07 * SR):int(j * 0.07 * SR) + len(b)] += b
        return out
    if kind == "score":
        out = np.zeros(int(3.6 * SR))
        times = np.cumsum(np.linspace(0.11, 0.03, 22))
        for tk in times:
            b = beep(1800, 0.02, decay=0.008) * 0.12
            out[int(tk * SR):int(tk * SR) + len(b)] += b
        tt = np.arange(int(2.2 * SR)) / SR
        sting = sum(np.sin(2 * np.pi * midi(n) * tt) for n in (57, 64, 69, 71, 76)) * np.exp(-tt / 0.9) * 0.08
        out[int(1.3 * SR):int(1.3 * SR) + len(sting)] += sting
        return out
    return np.zeros(1)


def render(cues, seconds):
    mix = music(seconds)
    for c in cues:
        x = sfx(c["type"], c)
        place(mix, x, c["t"], gain=1.0)
    # master: gentle bus glue, loudness to about -14 LUFS (RMS proxy), true-peak safety
    mix = hp(mix, 30)
    rms = np.sqrt(np.mean(mix ** 2)) + 1e-9
    mix *= 10 ** (-17.0 / 20) / rms
    mix = np.tanh(mix * 1.15) / np.tanh(1.15)
    peak = np.max(np.abs(mix))
    if peak > 0.89:
        mix *= 0.89 / peak
    return mix


def write_wav(path, stereo):
    data = (np.clip(stereo.T, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(data.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cues")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=108)
    a = ap.parse_args()
    cues = json.load(open(a.cues, encoding="utf-8"))
    write_wav(a.out, render(cues, a.seconds))
    print(f"wrote {a.out} ({len(cues)} cues)")


if __name__ == "__main__":
    main()
