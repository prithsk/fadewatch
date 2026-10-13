"""Render the guided tour to an MP4, frame by frame.

    fadewatch serve &            # or any static server on web/
    python scripts/record_demo.py --url http://127.0.0.1:8765/ --out demo.mp4

The page runs in capture mode: its clock only advances when this script
calls advance(1/fps), so the video is smooth no matter how slowly frames
render. Needs `pip install playwright imageio-ffmpeg` and
`python -m playwright install chromium`.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import time

import imageio_ffmpeg
from playwright.async_api import async_playwright


GL_ARGS = {
    "gpu": ["--use-gl=angle", "--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"],
    "cpu": ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
}


async def record(url: str, out: str, fps: int, seconds: float, width: int, height: int, scale: float,
                 gl: str = "gpu") -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.Popen([
        ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps), "-i", "-",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out,
    ], stdin=subprocess.PIPE)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=GL_ARGS[gl])
        page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
        page.on("pageerror", lambda e: print("page error:", e))
        await page.goto(url + ("&" if "?" in url else "?") + "tour&capture", wait_until="networkidle")
        await page.wait_for_function("window.fadewatch && window.fadewatch.ready")
        await page.wait_for_timeout(1500)
        n = int(seconds * fps)
        t0 = time.time()
        for i in range(n):
            await page.evaluate(f"window.fadewatch.advance({1 / fps})")
            await page.wait_for_timeout(8)  # let the globe's own render loop draw the new state
            proc.stdin.write(await page.screenshot(type="jpeg", quality=92))
            if i % (fps * 5) == 0:
                el = time.time() - t0
                print(f"frame {i}/{n}  {el:.0f}s elapsed, ~{el / max(i, 1) * (n - i):.0f}s left", flush=True)
        await browser.close()
    proc.stdin.close()
    proc.wait()
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/")
    ap.add_argument("--out", default="fadewatch-demo.mp4")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=121)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--scale", type=float, default=1.2)
    ap.add_argument("--gl", choices=["gpu", "cpu"], default="gpu", help="gpu: D3D11 via ANGLE; cpu: SwiftShader")
    a = ap.parse_args()
    asyncio.run(record(a.url, a.out, a.fps, a.seconds, a.width, a.height, a.scale, a.gl))


if __name__ == "__main__":
    main()
