"""Generate the USB Bridge launcher icon.

Emits an adaptive-icon vector foreground plus legacy PNG mipmaps for API 24-25,
all from the same design-space geometry so the vector and the bitmaps stay identical.

Design: two device nodes (phone left, PC right) joined by a bidirectional arrow
- i.e. a bridge between a phone and a PC.

Requires Pillow (pip install Pillow). Run from the repo root:

    python tools/gen_launcher_icons.py android/app/src/main/res

Re-running overwrites res/drawable/ic_launcher_{foreground,background}.xml and
the res/mipmap-*/ bitmaps. The <monochrome> layer is emitted here too, so
regenerating does not lose it.
"""

import os
import sys

from PIL import Image, ImageDraw

# ---- Design space -----------------------------------------------------------------
# Adaptive icons use a 108x108 viewport where only the centre ~72x72 is guaranteed
# visible after the launcher applies its mask, so all art stays inside 24..84.
DESIGN = 108.0

PHONE = (26.0, 38.0, 44.0, 70.0, 4.5)   # x1, y1, x2, y2, corner radius
PC = (64.0, 46.0, 82.0, 64.0, 3.5)

# Right-pointing arrow: shaft rect + triangular head
ARROW_R_SHAFT = (46.0, 48.0, 58.0, 52.0)
ARROW_R_HEAD = ((63.0, 50.0), (57.0, 45.0), (57.0, 55.0))

# Left-pointing arrow
ARROW_L_SHAFT = (50.0, 56.0, 62.0, 60.0)
ARROW_L_HEAD = ((45.0, 58.0), (51.0, 53.0), (51.0, 63.0))

GLYPH_BBOX = (25.0, 38.0, 83.0, 70.0)

BG_TOP = (0x00, 0x89, 0x7B)
BG_BOTTOM = (0x00, 0x4D, 0x40)
FG = (0xFF, 0xFF, 0xFF)

# Legacy icons are drawn with the glyph at ~70% of the canvas, roughly how much of an
# adaptive icon survives masking. Verified to stay inside the round variant's circle.
LEGACY_GLYPH_FRACTION = 0.70
LEGACY_CORNER_FRACTION = 0.22
SUPERSAMPLE = 8

DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}


# ---- Vector (adaptive foreground) -------------------------------------------------

def rounded_rect_path(x1, y1, x2, y2, r):
    return (
        f"M{x1 + r},{y1} L{x2 - r},{y1} A{r},{r} 0 0 1 {x2},{y1 + r} "
        f"L{x2},{y2 - r} A{r},{r} 0 0 1 {x2 - r},{y2} "
        f"L{x1 + r},{y2} A{r},{r} 0 0 1 {x1},{y2 - r} "
        f"L{x1},{y1 + r} A{r},{r} 0 0 1 {x1 + r},{y1} Z"
    )


def rect_path(x1, y1, x2, y2):
    return f"M{x1},{y1} L{x2},{y1} L{x2},{y2} L{x1},{y2} Z"


def tri_path(points):
    (ax, ay), (bx, by), (cx, cy) = points
    return f"M{ax},{ay} L{bx},{by} L{cx},{cy} Z"


def fmt(v):
    """Trim trailing .0 so the emitted path data stays readable."""
    return ("%f" % v).rstrip("0").rstrip(".")


def remap(path):
    import re

    def sub(m):
        return fmt(float(m.group(0)))

    return re.sub(r"-?\d+\.\d+", sub, path)


def build_foreground_vector():
    subpaths = [
        rounded_rect_path(*PHONE),
        rounded_rect_path(*PC),
        rect_path(*ARROW_R_SHAFT),
        tri_path(ARROW_R_HEAD),
        rect_path(*ARROW_L_SHAFT),
        tri_path(ARROW_L_HEAD),
    ]
    # One path, fillType nonZero: the shapes do not overlap so a single path is fine.
    data = remap(" ".join(subpaths))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!--
  Adaptive launcher icon foreground. Generated from the same geometry as the legacy
  mipmap PNGs; safe zone is the centre 72x72 of the 108x108 viewport.
-->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path
        android:fillColor="#FFFFFFFF"
        android:pathData="{data}" />
</vector>
'''


def build_adaptive_xml():
    # ic_launcher and ic_launcher_round share one definition; the launcher picks the mask.
    return f'''<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background" />
    <foreground android:drawable="@drawable/ic_launcher_foreground" />
    <!-- Lets Android 13+ tint the icon for themed ("Material You") launchers. -->
    <monochrome android:drawable="@drawable/ic_launcher_foreground" />
</adaptive-icon>
'''


def build_background_drawable():
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!-- Brand gradient behind the adaptive icon foreground. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:aapt="http://schemas.android.com/aapt"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <path android:pathData="M0,0 L108,0 L108,108 L0,108 Z">
        <aapt:attr name="android:fillColor">
            <gradient
                android:startX="0" android:startY="0"
                android:endX="0" android:endY="108"
                android:type="linear">
                <item android:offset="0" android:color="#FF00897B" />
                <item android:offset="1" android:color="#FF004D40" />
            </gradient>
        </aapt:attr>
    </path>
</vector>
'''


# ---- Legacy bitmaps ---------------------------------------------------------------

def scale_point(x, y, scale, ox, oy):
    return (x * scale + ox, y * scale + oy)


def draw_legacy(size, round_icon):
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background: vertical gradient, masked to a squircle or a circle.
    grad = Image.new("RGBA", (1, big))
    gd = ImageDraw.Draw(grad)
    for y in range(big):
        t = y / max(big - 1, 1)
        gd.point((0, y), fill=tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)) + (255,))
    grad = grad.resize((big, big))

    mask = Image.new("L", (big, big), 0)
    md = ImageDraw.Draw(mask)
    if round_icon:
        md.ellipse((0, 0, big - 1, big - 1), fill=255)
    else:
        radius = int(big * LEGACY_CORNER_FRACTION)
        md.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=255)

    img.paste(grad, (0, 0), mask)

    # Glyph: fit GLYPH_BBOX to LEGACY_GLYPH_FRACTION of the canvas, centred.
    bx1, by1, bx2, by2 = GLYPH_BBOX
    gw, gh = bx2 - bx1, by2 - by1
    scale = (size * LEGACY_GLYPH_FRACTION) / gw * SUPERSAMPLE
    ox = (big - gw * scale) / 2 - bx1 * scale
    oy = (big - gh * scale) / 2 - by1 * scale

    def p(x, y):
        return scale_point(x, y, scale, ox, oy)

    def put_rounded_rect(spec):
        x1, y1, x2, y2, r = spec
        d.rounded_rectangle(
            (p(x1, y1)[0], p(x1, y1)[1], p(x2, y2)[0], p(x2, y2)[1]),
            radius=r * scale, fill=FG)

    put_rounded_rect(PHONE)
    put_rounded_rect(PC)
    d.rectangle((p(*ARROW_R_SHAFT[:2])[0], p(*ARROW_R_SHAFT[:2])[1],
                 p(*ARROW_R_SHAFT[2:])[0], p(*ARROW_R_SHAFT[2:])[1]), fill=FG)
    d.polygon([p(x, y) for x, y in ARROW_R_HEAD], fill=FG)
    d.rectangle((p(*ARROW_L_SHAFT[:2])[0], p(*ARROW_L_SHAFT[:2])[1],
                 p(*ARROW_L_SHAFT[2:])[0], p(*ARROW_L_SHAFT[2:])[1]), fill=FG)
    d.polygon([p(x, y) for x, y in ARROW_L_HEAD], fill=FG)

    return img.resize((size, size), Image.LANCZOS)


def main():
    res = sys.argv[1]
    drawable = os.path.join(res, "drawable")
    anydpi = os.path.join(res, "mipmap-anydpi-v26")
    os.makedirs(drawable, exist_ok=True)
    os.makedirs(anydpi, exist_ok=True)

    with open(os.path.join(drawable, "ic_launcher_foreground.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(build_foreground_vector())
    with open(os.path.join(drawable, "ic_launcher_background.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(build_background_drawable())
    with open(os.path.join(anydpi, "ic_launcher.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(build_adaptive_xml())
    with open(os.path.join(anydpi, "ic_launcher_round.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(build_adaptive_xml())

    for density, size in DENSITIES.items():
        out = os.path.join(res, "mipmap-" + density)
        os.makedirs(out, exist_ok=True)
        for round_icon in (False, True):
            name = "ic_launcher_round.png" if round_icon else "ic_launcher.png"
            draw_legacy(size, round_icon).save(os.path.join(out, name))
        print(f"  mipmap-{density}: {size}x{size}")


if __name__ == "__main__":
    main()
