"""Render the guided tour to an MP4 with its synthesised soundtrack, frame by frame.

    fadewatch serve &            # or any static server on web/
    python scripts/record_demo.py --url http://127.0.0.1:8765/ --out docs/fadewatch-demo.mp4

The page runs in capture mode: its clock only advances when this script calls
advance(1/fps), so motion is smooth however slowly frames render. The page
records sound cues against tour time; scripts/soundtrack.py turns them into
the soundtrack, and ffmpeg muxes both. Needs `pip install playwright
imageio-ffmpeg scipy` and `python -m playwright install chromium`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

import imageio_ffmpeg
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soundtrack  # noqa: E402

GL_ARGS = {
    "gpu": ["--use-gl=angle", "--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"],
    "cpu": ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
}


async def capture(url, video, fps, seconds, width, height, scale, gl):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.Popen([ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "mjpeg",
                             "-framerate", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "17",
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", video], stdin=subprocess.PIPE)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=GL_ARGS[gl])
        page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
        page.on("pageerror", lambda e: print("page error:", e))
        await page.goto(url + ("&" if "?" in url else "?") + "tour&capture", wait_until="networkidle")
        await page.wait_for_function("window.fadewatch && window.fadewatch.ready", timeout=60000)
        await page.wait_for_timeout(1500)
        seconds = seconds or await page.evaluate("window.fadewatch.tourLength || 105")
        n = int(seconds * fps)
        t0 = time.time()
        for i in range(n):
            await page.evaluate(f"window.fadewatch.advance({1 / fps})")
            proc.stdin.write(await page.screenshot(type="jpeg", quality=93))
            if i % (fps * 5) == 0:
                el = time.time() - t0
                print(f"frame {i}/{n}  {el:.0f}s elapsed, ~{el / max(i, 1) * (n - i):.0f}s left", flush=True)
        cues = await page.evaluate("window.fadewatch.cues")
        await browser.close()
    proc.stdin.close()
    proc.wait()
    return cues, seconds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/")
    ap.add_argument("--out", default="fadewatch-demo.mp4")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=0, help="default: the tour's own length")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--scale", type=float, default=1.2)
    ap.add_argument("--gl", choices=["gpu", "cpu"], default="gpu", help="gpu: D3D11 via ANGLE; cpu: SwiftShader")
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="fadewatch-")
    video = os.path.join(tmp, "video.mp4")
    cues, seconds = asyncio.run(capture(a.url, video, a.fps, a.seconds, a.width, a.height, a.scale, a.gl))
    json.dump(cues, open(os.path.join(tmp, "cues.json"), "w"), indent=1)
    if a.no_audio:
        os.replace(video, a.out)
    else:
        wav = os.path.join(tmp, "audio.wav")
        soundtrack.write_wav(wav, soundtrack.render(cues, seconds))
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", video, "-i", wav,
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", a.out],
                       check=True)
    print(f"wrote {a.out} ({len(cues)} sound cues)")


if __name__ == "__main__":
    main()
