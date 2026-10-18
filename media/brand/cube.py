"""Build the 3D cube logo (the "a." die) as PNGs with Playwright and CSS 3D.

    python media/brand/cube.py

Small sizes (favicon, 32 px) keep the flat "a." from build.py: the cube's depth is not readable there.
"""

from __future__ import annotations

import base64
import glob
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "assets"
BLUE, DEEP, TOP, SUN = "#0070c9", "#005ea8", "#3d94db", "#ffd400"
FONT = base64.b64encode((HERE / "font" / "outfit-latin-700-normal.woff2").read_bytes()).decode()


def cube(size: int, face: str = BLUE) -> str:
    """A cube with edge `size`, front face `face`, viewed from above right."""
    h = size / 2
    return f"""
<div style="width:{size * 1.5:.0f}px;height:{size * 1.5:.0f}px;display:grid;place-items:center;perspective:{size * 8:.0f}px">
 <div style="position:relative;width:{size}px;height:{size}px;transform-style:preserve-3d;transform:rotateX(-22deg) rotateY(-34deg)">
  <div style="position:absolute;inset:0;border-radius:{size * 0.035:.0f}px;background:{face};transform:translateZ({h}px);overflow:hidden">
   <div style="position:absolute;right:{-size * 0.3:.0f}px;top:{-size * 0.3:.0f}px;width:{size * 0.68:.0f}px;height:{size * 0.68:.0f}px;border-radius:50%;background:{DEEP}"></div>
   <div style="position:absolute;left:{size * 0.24:.0f}px;top:{size * 0.06:.0f}px;font:700 {size * 0.66:.0f}px Outfit;color:#fff;line-height:1">a</div>
   <div style="position:absolute;left:{size * 0.67:.0f}px;top:{size * 0.6:.0f}px;width:{size * 0.15:.0f}px;height:{size * 0.15:.0f}px;border-radius:50%;background:{SUN}"></div>
  </div>
  <div style="position:absolute;inset:0;border-radius:{size * 0.035:.0f}px;background:{DEEP};transform:rotateY(90deg) translateZ({h}px)"></div>
  <div style="position:absolute;inset:0;border-radius:{size * 0.035:.0f}px;background:{TOP};transform:rotateX(90deg) translateZ({h}px)"></div>
 </div>
</div>"""


def page(body: str, bg: str, w: int, h: int, shade: bool = False) -> str:
    sh = (
        f'<div style="position:absolute;right:{-w * 0.25:.0f}px;top:{-w * 0.25:.0f}px;width:{w * 0.75:.0f}px;height:{w * 0.75:.0f}px;border-radius:50%;background:{DEEP};opacity:.55"></div>'
        if shade
        else ""
    )
    return (
        f"<html><head><style>@font-face{{font-family:Outfit;font-weight:700;src:url(data:font/woff2;base64,{FONT})}}"
        f"body{{margin:0;width:{w}px;height:{h}px;background:{bg};display:grid;place-items:center;overflow:hidden;position:relative}}</style></head>"
        f'<body>{sh}<div style="position:relative">{body}</div></body></html>'
    )


def main() -> None:
    from playwright.sync_api import sync_playwright

    jobs = [
        ("agentl-cube-256.png", page(cube(118), "transparent", 256, 256), 256, 256),
        ("agentl-cube-onblue-1024.png", page(cube(470, "#1a85dc"), "transparent", 1024, 1024), 1024, 1024),
        ("agentl-cube-1024.png", page(cube(470), "transparent", 1024, 1024), 1024, 1024),
        ("agentl-cube-avatar-white-1000.png", page(cube(400), "#ffffff", 1000, 1000), 1000, 1000),
        (
            "agentl-cube-avatar-blue-1000.png",
            page(cube(400, "#1a85dc"), BLUE, 1000, 1000, shade=True),
            1000,
            1000,
        ),
    ]
    exe = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome") or [None])[0]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        for name, html, w, h in jobs:
            tab = browser.new_page(viewport={"width": w, "height": h})
            tab.set_content(html)
            tab.wait_for_timeout(200)
            tab.screenshot(path=str(OUT / name), omit_background=True)
            print(OUT / name)
        browser.close()


if __name__ == "__main__":
    main()
