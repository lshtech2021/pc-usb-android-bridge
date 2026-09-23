"""Rasterise the app's vector drawables to a PNG contact sheet.

The UI is built programmatically and there is no easy way to eyeball a
hand-authored <vector> without a device, so this renders them off-device.
It supports the subset of path syntax these icons use: absolute and relative
M/L/H/V/C/Z, with fills and strokes.

Run from the repo root to lay out every ic_*.xml in a directory:

    python tools/render_icons.py android/app/src/main/res/drawable icons.png

It also exposes render(path, size) for building custom sheets.

Requires Pillow (pip install Pillow). Note it ignores <group> transforms and
gradient fills, so drawables using those (ic_launcher_background) come out blank.
"""

import re
import sys

from PIL import Image, ImageDraw

SIZE = 96
SS = 4  # supersample
BG = (250, 250, 250, 255)


def parse_color(c):
    if not c:
        return None
    c = c.lstrip("#")
    if len(c) == 8:
        return (int(c[2:4], 16), int(c[4:6], 16), int(c[6:8], 16), int(c[0:2], 16))
    if len(c) == 6:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255)
    return None


def attr(attrs, name, default=None):
    m = re.search(r'android:%s="([^"]*)"' % name, attrs)
    return m.group(1) if m else default


def parse_paths(xml):
    out = []
    for m in re.finditer(r"<path\b(.*?)/>", xml, re.S):
        a = m.group(1)
        out.append({
            "d": attr(a, "pathData", "") or "",
            "fill": parse_color(attr(a, "fillColor")),
            "stroke": parse_color(attr(a, "strokeColor")),
            "sw": float(attr(a, "strokeWidth", "0") or 0),
        })
    return out


CURVE_STEPS = 24


def _bezier(p0, p1, p2, p3):
    pts = []
    for s in range(1, CURVE_STEPS + 1):
        t = s / CURVE_STEPS
        u = 1 - t
        pts.append((
            u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ))
    return pts


def to_subpaths(d):
    """Absolute+relative M/L/H/V/C/Z, enough for the icons in this app."""
    toks = re.findall(r"[A-Za-z]|-?\d*\.?\d+", d)
    subs, cur, i = [], [], 0
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    cmd = None
    nums = []

    def flush():
        nonlocal cur
        if cur:
            subs.append(cur)
            cur = []

    while i < len(toks):
        t = toks[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append(start)
                    flush()
            continue
        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            x, y = float(toks[i]), float(toks[i + 1]); i += 2
            pos = (pos[0] + x, pos[1] + y) if rel else (x, y)
            flush()
            cur = [pos]
            start = pos
            cmd = "l" if rel else "L"   # subsequent pairs are implicit lineto
        elif c == "L":
            x, y = float(toks[i]), float(toks[i + 1]); i += 2
            pos = (pos[0] + x, pos[1] + y) if rel else (x, y)
            cur.append(pos)
        elif c == "H":
            x = float(toks[i]); i += 1
            pos = (pos[0] + x, pos[1]) if rel else (x, pos[1])
            cur.append(pos)
        elif c == "V":
            y = float(toks[i]); i += 1
            pos = (pos[0], pos[1] + y) if rel else (pos[0], y)
            cur.append(pos)
        elif c == "C":
            v = [float(toks[i + k]) for k in range(6)]; i += 6
            if rel:
                p1 = (pos[0] + v[0], pos[1] + v[1])
                p2 = (pos[0] + v[2], pos[1] + v[3])
                end = (pos[0] + v[4], pos[1] + v[5])
            else:
                p1, p2, end = (v[0], v[1]), (v[2], v[3]), (v[4], v[5])
            cur.extend(_bezier(pos, p1, p2, end))
            pos = end
        else:
            i += 1
    flush()
    return subs


def render(path, size=SIZE):
    big = size * SS
    scale = big / 24.0
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    open_paths = []
    for p in parse_paths(open(path, encoding="utf-8").read()):
        for sp in to_subpaths(p["d"]):
            pts = [(x * scale, y * scale) for x, y in sp]
            closed = len(sp) > 2 and sp[0] == sp[-1]
            if p["fill"] and p["fill"][3] > 0 and closed:
                d.polygon(pts, fill=p["fill"])
            stroke_col = p["stroke"] if (p["stroke"] and p["stroke"][3] > 0
                                        and p["sw"] > 0) else (
                p["fill"] if p["fill"] and p["fill"][3] > 0 and not closed else None)
            if stroke_col:
                w = max(2, int((p["sw"] or 1.6) * scale))
                d.line(pts, fill=stroke_col, width=w, joint="curve")
                open_paths.append((pts, stroke_col, w))
    # Round-ish caps: stamp a disc at each end of stroked polylines.
    for pts, col, w in open_paths:
        for end in (pts[0], pts[-1]):
            r = w / 2
            d.ellipse((end[0] - r, end[1] - r, end[0] + r, end[1] + r), fill=col)
    img = Image.alpha_composite(img, layer)
    return img.resize((size, size), Image.LANCZOS)


def main():
    src, out = sys.argv[1], sys.argv[2]
    import os
    names = sorted(n for n in os.listdir(src) if n.startswith("ic_") and n.endswith(".xml"))
    cols = 4
    rows = (len(names) + cols - 1) // cols
    pad = 12
    W = cols * (SIZE + pad) + pad
    H = rows * (SIZE + pad) + pad
    sheet = Image.new("RGBA", (W, H), BG)
    for idx, n in enumerate(names):
        r, c = divmod(idx, cols)
        x = pad + c * (SIZE + pad)
        y = pad + r * (SIZE + pad)
        sheet.alpha_composite(render(os.path.join(src, n)), (x, y))
    sheet.save(out)
    print(f"{len(names)} icons -> {out}")
    for i, n in enumerate(names):
        print(f"  [{i}] {n}")


if __name__ == "__main__":
    main()
