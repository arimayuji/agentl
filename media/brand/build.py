"""Build the agentl brand assets (Carioca palette) from the Outfit Bold font.

Text is converted to outlines, so the SVGs render the same everywhere
without the font installed. PNGs are rendered with Playwright.

    pip install fonttools brotli playwright
    python media/brand/build.py
"""

from __future__ import annotations

import base64
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

# Brand palette: blue, yellow and white (Brazil). Green and red appear only as status colors.
WHITE = "#ffffff"
BLUE = "#0070c9"
BLUE_DEEP = "#005ea8"  # depth on blue backgrounds
SUN = "#ffd400"
INK = "#0b1b2b"  # text on light backgrounds
NAVY = "#08131f"  # dark backgrounds

INK_DARK = WHITE  # wordmark on dark backgrounds
INK_LIGHT = INK  # wordmark on light backgrounds


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


def _sun(x: float, baseline: float, size: float) -> str:
    """The sun dot that closes the name, sitting on the baseline like a period."""
    r = size * 0.075
    return f'<circle cx="{x + r * 1.6:.1f}" cy="{baseline - r:.1f}" r="{r:.1f}" fill="{SUN}"/>'


def wordmark(color: str, size: float = 100, pad: float = 0) -> str:
    d, w, top, bottom = outline("agentl", size)
    r = size * 0.075
    full = w + r * 2.6
    h = bottom - top
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad} {top - pad} {full + 2 * pad:.1f} {h + 2 * pad:.1f}" '
        f'width="{full + 2 * pad:.0f}" height="{h + 2 * pad:.0f}" role="img" aria-label="agentl">'
        f'<path d="{d}" fill="{color}"/>{_sun(w, 0, size)}</svg>'
    )


def _mark(size: float, scale: float) -> str:
    """The "a." centred in a square of `size`."""
    d, w, top, bottom = outline("a", size * scale)
    dot = size * scale * 0.075 * 2.6
    gx = (size - w - dot) / 2
    gy = (size - (bottom - top)) / 2 - top
    return f'<g transform="translate({gx:.1f} {gy:.1f})"><path d="{d}" fill="{WHITE}"/>{_sun(w, 0, size * scale)}</g>'


def _shade(width: float, height: float) -> str:
    """The deep blue circle from the top right corner: depth on blue without a new color."""
    return f'<circle cx="{width:.0f}" cy="0" r="{height * 0.62:.0f}" fill="{BLUE_DEEP}" opacity=".55"/>'


def icon(size: int = 512) -> str:
    """App icon and favicon: rounded square."""
    rx = f"{size * 0.22:.0f}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
        f'<defs><clipPath id="r"><rect width="{size}" height="{size}" rx="{rx}"/></clipPath></defs>'
        f'<g clip-path="url(#r)"><rect width="{size}" height="{size}" fill="{BLUE}"/>{_shade(size, size)}</g>'
        f"{_mark(size, 0.62)}</svg>"
    )


def avatar(size: int = 512) -> str:
    """Profile picture: full bleed, since X, GitHub and others crop it to a circle."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
        f'<rect width="{size}" height="{size}" fill="{BLUE}"/>{_shade(size, size)}{_mark(size, 0.56)}</svg>'
    )


def _cube_image(x: float, y: float, size: float) -> str:
    """The 3D cube logo (rendered by cube.py) as an embedded image, or nothing if it is missing."""
    png = OUT / "agentl-cube-onblue-1024.png"  # lighter front face, readable on the blue
    if not png.exists():
        return ""
    data = base64.b64encode(png.read_bytes()).decode()
    return f'<image x="{x:.1f}" y="{y:.1f}" width="{size:.1f}" height="{size:.1f}" href="data:image/png;base64,{data}"/>'


def _wide(width: int, height: int, title: float, tagline: str, x_center: float, cube: bool = False) -> str:
    d, w, top, bottom = outline("agentl", title)
    r = title * 0.075
    full = w + r * 2.6
    cube_size = title * 1.9 if cube else 0.0
    gx = x_center - full / 2 + cube_size * 0.42
    gy = height / 2 - title * 0.2 - (top + bottom) / 2
    tag, tw, _t, _b = outline(tagline, title * 0.16, TEXT_FONT, 0.01)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="{BLUE}"/>'
        f"{_shade(width, height)}"
        + (_cube_image(gx - cube_size * 0.95, gy + top - cube_size * 0.28, cube_size) if cube else "")
        + f'<g transform="translate({gx:.1f} {gy:.1f})"><path d="{d}" fill="{WHITE}"/>{_sun(w, 0, title)}</g>'
        f'<path transform="translate({gx + full / 2 - tw / 2:.1f} {gy + bottom + title * 0.38:.1f})" d="{tag}" fill="{WHITE}" '
        'fill-opacity=".85"/>'
        "</svg>"
    )


def social(width: int = 1280, height: int = 640) -> str:
    """GitHub / Open Graph preview."""
    return _wide(width, height, 210, "Agentic ETL for Python", width / 2)


def banner(width: int = 1500, height: int = 500) -> str:
    """X header. The profile picture covers the lower left, so the text sits right of center."""
    return _wide(width, height, 170, "Agentic ETL in YAML · pip install agentl-etl", width * 0.58)


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
        "agentl-avatar.svg": avatar(),
        "agentl-social.svg": social(),
        "agentl-banner.svg": banner(),
    }
    for old in ("agentl-icon-light.svg",):  # retired with the Carioca palette
        (OUT / old).unlink(missing_ok=True)
    for name, svg in files.items():
        (OUT / name).write_text(svg)
    pngs = [
        ("agentl-icon", 512, 512, "agentl-icon-512.png"),
        ("agentl-icon", 180, 180, "agentl-icon-180.png"),
        ("agentl-icon", 32, 32, "agentl-icon-32.png"),
        ("agentl-avatar", 400, 400, "agentl-avatar-400.png"),
        ("agentl-avatar", 1000, 1000, "agentl-avatar-1000.png"),
        ("agentl-social", 1280, 640, "agentl-social-1280x640.png"),
        ("agentl-banner", 1500, 500, "agentl-banner-1500x500.png"),
    ]
    for src, w, h, png in pngs:
        render_png(OUT / f"{src}.svg", OUT / png, w, h)
    for name in ("agentl-wordmark-dark", "agentl-wordmark-light"):
        svg = (OUT / f"{name}.svg").read_text()
        w, h = (float(v) for v in svg.split('width="')[1].split('"')[0:3:2])
        render_png(OUT / f"{name}.svg", OUT / f"{name}.png", int(w * 4), int(h * 4))
    # the docs site serves its own copies
    docs = HERE.parent.parent / "docs" / "assets"
    for name in (
        "agentl-wordmark-dark.svg",
        "agentl-wordmark-light.svg",
        "agentl-icon-180.png",
        "agentl-social-1280x640.png",
        "agentl-cube-256.png",
    ):
        (docs / name).write_bytes((OUT / name).read_bytes())
    (docs / "favicon.svg").write_text(files["agentl-icon.svg"])
    print("\n".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
