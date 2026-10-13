"""Build the agentl brand assets from the Outfit Bold font.

Text is converted to outlines, so the SVGs render the same everywhere
without the font installed. PNGs are rendered with Playwright.

    pip install fonttools brotli playwright
    python media/brand/build.py
"""

from __future__ import annotations

import glob
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent
FONT = TTFont(HERE / "font" / "outfit-latin-700-normal.woff2")
TEXT_FONT = TTFont(HERE / "font" / "inter-latin-400-normal.woff2")  # supporting text
OUT = HERE / "assets"
TRACKING = -0.03  # em, slightly tighter than default

INK_DARK = "#f4f4f6"  # on dark backgrounds
INK_LIGHT = "#0b0b10"  # on light backgrounds
BG_DARK = "#07070b"


def outline(
    text: str, size: float, font: TTFont = FONT, tracking: float = TRACKING
) -> tuple[str, float, float, float]:
    """Return (svg path, width, top, bottom) for `text` at `size` px."""
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    scale = size / upm
    pen = SVGPathPen(glyphs)
    bounds = BoundsPen(glyphs)
    x = 0.0
    for ch in text:
        name = cmap[ord(ch)]
        # Flip y (font units point up) and place each glyph at its advance.
        t = (scale, 0, 0, -scale, x * scale, 0)
        glyphs[name].draw(TransformPen(pen, t))
        glyphs[name].draw(TransformPen(bounds, t))
        x += glyphs[name].width + tracking * upm
    _x_min, y_min, x_max, y_max = bounds.bounds
    return pen.getCommands(), x_max, y_min, y_max


def wordmark(color: str, size: float = 100, pad: float = 0) -> str:
    d, w, top, bottom = outline("agentl", size)
    h = bottom - top
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad} {top - pad} {w + 2 * pad:.1f} {h + 2 * pad:.1f}" '
        f'width="{w + 2 * pad:.0f}" height="{h + 2 * pad:.0f}" role="img" aria-label="agentl">'
        f'<path d="{d}" fill="{color}"/></svg>'
    )


def icon(size: int = 512, bg: str = BG_DARK, ink: str = INK_DARK) -> str:
    d, w, top, bottom = outline("a", size * 0.62)
    gx = (size - w) / 2
    gy = (size - (bottom - top)) / 2 - top
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
        f'<rect width="{size}" height="{size}" rx="{size * 0.22:.0f}" fill="{bg}"/>'
        f'<path transform="translate({gx:.1f} {gy:.1f})" d="{d}" fill="{ink}"/></svg>'
    )


BLOBS = [  # (cx, cy, r, color) of the soft color mesh behind the social image
    (0.02, 0.05, 0.26, "#c4b5fd"),
    (0.38, -0.12, 0.22, "#a5f3fc"),
    (0.95, 0.02, 0.26, "#fbcfe8"),
    (1.02, 0.95, 0.27, "#99f6e4"),
    (0.58, 1.14, 0.24, "#fde68a"),
    (0.00, 1.02, 0.24, "#bfdbfe"),
]


def social(width: int = 1280, height: int = 640) -> str:
    d, w, top, bottom = outline("agentl", 210)
    gx = (width - w) / 2
    gy = height / 2 - 40 - (top + bottom) / 2
    tag, tw, _ttop, _tbot = outline("Agentic ETL for Python", 34, TEXT_FONT, 0.01)
    blobs = "".join(
        f'<circle cx="{x * width:.0f}" cy="{y * height:.0f}" r="{r * width:.0f}" fill="{col}"/>'
        for x, y, r, col in BLOBS
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        '<defs><filter id="soft" x="-50%" y="-50%" width="200%" height="200%">'
        '<feGaussianBlur stdDeviation="90"/></filter></defs>'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f'<g filter="url(#soft)" opacity=".9">{blobs}</g>'
        f'<path transform="translate({gx:.1f} {gy:.1f})" d="{d}" fill="{INK_LIGHT}"/>'
        f'<path transform="translate({(width - tw) / 2:.1f} {gy + bottom + 80:.1f})" d="{tag}" fill="#3f3f46"/>'
        "</svg>"
    )


def render_png(svg_path: Path, png_path: Path, width: int, height: int) -> None:
    from playwright.sync_api import sync_playwright

    exe = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome") or [None])[0]
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_content(
            f'<html><body style="margin:0;background:transparent">'
            f'<img src="data:image/svg+xml;base64,{__import__("base64").b64encode(svg_path.read_bytes()).decode()}" '
            f'style="width:{width}px;height:{height}px;display:block"></body></html>'
        )
        page.screenshot(path=str(png_path), omit_background=True)
        browser.close()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = {
        "agentl-wordmark-dark.svg": wordmark(INK_DARK),  # for dark backgrounds
        "agentl-wordmark-light.svg": wordmark(INK_LIGHT),  # for light backgrounds
        "agentl-icon.svg": icon(),
        "agentl-icon-light.svg": icon(bg="#ffffff", ink=INK_LIGHT),
        "agentl-social.svg": social(),
    }
    for name, svg in files.items():
        (OUT / name).write_text(svg)
    render_png(OUT / "agentl-icon.svg", OUT / "agentl-icon-512.png", 512, 512)
    render_png(OUT / "agentl-icon.svg", OUT / "agentl-icon-180.png", 180, 180)
    render_png(OUT / "agentl-icon.svg", OUT / "agentl-icon-32.png", 32, 32)
    render_png(OUT / "agentl-social.svg", OUT / "agentl-social-1280x640.png", 1280, 640)
    for name in ("agentl-wordmark-dark", "agentl-wordmark-light"):
        svg = (OUT / f"{name}.svg").read_text()
        w, h = (float(v) for v in svg.split('width="')[1].split('"')[0:3:2])
        render_png(OUT / f"{name}.svg", OUT / f"{name}.png", int(w * 4), int(h * 4))
    print("\n".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
