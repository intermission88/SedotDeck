"""Builds the SedotDeck logo kit (concept B, "Deck Sip") into design/logo/kit/.

Everything is plain geometry on a 256 grid: a handheld body with a screen and two stick holes
(evenodd cut-outs), a straw expanded to a filled outline, and a monoline wordmark drawn with the
same round-ended stroke language (no font, so no font licence).
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "kit")
os.makedirs(OUT, exist_ok=True)

# Palette (matches the SteamOS-style UI)
INK = "#0f1115"        # body on light backgrounds
LIGHT = "#e6e8eb"      # body on dark backgrounds
BLUE = "#1a9fff"       # straw on dark backgrounds / app icon
BLUE_DEEP = "#1475c4"  # straw on light backgrounds (contrast on white)
TILE = "#1a1d23"       # app icon tile


def f(n):
    return ("%.2f" % n).rstrip("0").rstrip(".")


def stadium_rect(x0, y0, x1, y1, r):
    return ("M%s %sH%sA%s %s 0 0 1 %s %sV%sA%s %s 0 0 1 %s %sH%sA%s %s 0 0 1 %s %sV%sA%s %s 0 0 1 %s %sZ" % (
        f(x0 + r), f(y0), f(x1 - r), f(r), f(r), f(x1), f(y0 + r), f(y1 - r), f(r), f(r), f(x1 - r), f(y1),
        f(x0 + r), f(r), f(r), f(x0), f(y1 - r), f(y0 + r), f(r), f(r), f(x0 + r), f(y0)))


def rect_hole(x0, y0, x1, y1, r):
    return stadium_rect(x0, y0, x1, y1, r)  # the body uses evenodd, so direction doesn't matter


def circle_hole(cx, cy, r):
    return "M%s %sA%s %s 0 1 0 %s %sA%s %s 0 1 0 %s %sZ" % (
        f(cx + r), f(cy), f(r), f(r), f(cx - r), f(cy), f(r), f(r), f(cx + r), f(cy))


def straw(x, y_bottom, y_bend, bend_r, length, w):
    """Filled outline of a straw: up from (x, y_bottom) to y_bend, a 45° right-hand bend, then straight."""
    h = w / 2
    c = (x + bend_r, y_bend)
    d = (math.sqrt(0.5), -math.sqrt(0.5))           # direction after the bend
    k = math.sqrt(0.5)
    mid = (c[0] - bend_r * k, c[1] - bend_r * k)    # centreline point where the bend ends
    tip = (mid[0] + length * d[0], mid[1] + length * d[1])
    ri, ro = bend_r - h, bend_r + h
    in_end = (c[0] - ri * k, c[1] - ri * k)
    out_end = (c[0] - ro * k, c[1] - ro * k)
    tr = (tip[0] + h * k, tip[1] + h * k)           # right edge at the tip
    tl = (tip[0] - h * k, tip[1] - h * k)           # left edge at the tip
    return ("M%s %sV%sA%s %s 0 0 1 %s %sL%s %sA%s %s 0 0 0 %s %sL%s %sA%s %s 0 0 0 %s %sV%sZ" % (
        f(x + h), f(y_bottom), f(y_bend), f(ri), f(ri), f(in_end[0]), f(in_end[1]), f(tr[0]), f(tr[1]),
        f(h), f(h), f(tl[0]), f(tl[1]), f(out_end[0]), f(out_end[1]), f(ro), f(ro), f(x - h), f(y_bend),
        f(y_bottom)))


# ---- the mark, regular and small-size cut
def mark_paths(small=False):
    if small:  # fewer, bigger parts for 16-32 px: no stick holes, wider screen gap, thicker straw
        body = stadium_rect(16, 120, 240, 220, 44) + rect_hole(78, 140, 178, 200, 6)
        st = straw(146, 122, 96, 20, 40, 34)
    else:
        body = (stadium_rect(24, 112, 232, 208, 40) + rect_hole(84, 130, 172, 190, 5)
                + circle_hole(55, 150, 11) + circle_hole(201, 150, 11))
        st = straw(140, 114, 86, 16, 48, 22)
    return body, st


def svg(view, body, title="SedotDeck"):
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s"><title>%s</title>%s</svg>\n' % (view, title, body)


def write(name, content):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        fh.write(content)
    print("wrote kit/" + name)


def mark_group(body_fill, straw_fill, small=False, dx=0, dy=0, scale=1):
    body, st = mark_paths(small)
    tr = ' transform="translate(%s %s) scale(%s)"' % (f(dx), f(dy), f(scale)) if (dx or dy or scale != 1) else ""
    return '<g%s><path fill="%s" fill-rule="evenodd" d="%s"/><path fill="%s" d="%s"/></g>' % (
        tr, body_fill, body, straw_fill, st)


# ---- monoline wordmark (stroke-based by design; round caps match the straw)
SW, B, XH = 17, 176, 72
H = SW / 2
R = (XH - SW) / 2                  # bowl radius on the centreline
CY = B - XH / 2                    # bowl centre
TOP = B - 100 + H                  # cap/ascender top on the centreline
XT = B - XH + H                    # x-height top on the centreline
BOT = B - H


def arc(r, large, sweep, x, y):
    return "A%s %s 0 %d %d %s %s" % (f(r), f(r), large, sweep, f(x), f(y))


def glyphs():
    out, x = [], 0.0
    gap = 9

    def add(d, width):
        nonlocal x
        out.append(d)
        x += width + gap

    # S
    r = (BOT - TOP) / 4
    cx, c1, c2 = x + H + r, TOP + r, TOP + 3 * r
    a = math.radians(25)
    add("M%s %s" % (f(cx + r * math.cos(a)), f(c1 - r * math.sin(a))) + arc(r, 1, 0, cx, c1 + r)
        + arc(r, 1, 1, cx - r * math.cos(a), c2 + r * math.sin(a)), 2 * r + SW)
    # e
    cx = x + H + R
    a = math.radians(42)
    add("M%s %sH%s" % (f(cx - R), f(CY), f(cx + R)) + arc(R, 1, 0, cx + R * math.cos(a), CY + R * math.sin(a)), 2 * R + SW)
    # d
    cx = x + H + R
    add("M%s %s" % (f(cx + R), f(CY)) + arc(R, 0, 1, cx - R, CY) + arc(R, 0, 1, cx + R, CY)
        + "M%s %sV%s" % (f(cx + R), f(TOP), f(BOT)), 2 * R + SW)
    # o
    cx = x + H + R
    add("M%s %s" % (f(cx + R), f(CY)) + arc(R, 0, 1, cx - R, CY) + arc(R, 0, 1, cx + R, CY), 2 * R + SW)
    # t
    x0, hook = x + H + 12, 16
    add("M%s %sV%s" % (f(x0), f(TOP + 10), f(BOT - hook)) + arc(hook, 0, 0, x0 + hook, BOT)
        + "M%s %sH%s" % (f(x0 - 12), f(XT), f(x0 + 22)), 34 + SW - 4)
    x += 2
    # D
    x0, r = x + H, (BOT - TOP) / 2
    add("M%s %sV%sH%s" % (f(x0), f(TOP), f(BOT), f(x0 + 18)) + arc(r, 0, 0, x0 + 18, TOP) + "Z", 18 + r + SW)
    # e
    cx = x + H + R
    a = math.radians(42)
    add("M%s %sH%s" % (f(cx - R), f(CY), f(cx + R)) + arc(R, 1, 0, cx + R * math.cos(a), CY + R * math.sin(a)), 2 * R + SW)
    # c
    cx = x + H + R
    a = math.radians(42)
    add("M%s %s" % (f(cx + R * math.cos(a)), f(CY - R * math.sin(a))) + arc(R, 1, 0, cx + R * math.cos(a), CY + R * math.sin(a)),
        R * (1 + math.cos(a)) + SW)
    # k
    x0 = x + H
    j = (x0 + 4, B - XH * 0.42)
    add("M%s %sV%sM%s %sL%s %sL%s %s" % (f(x0), f(TOP), f(BOT), f(x0 + 34), f(XT), f(j[0]), f(j[1]), f(x0 + 36), f(BOT)), 36 + SW)
    return " ".join(out), x - gap


def wordmark(color, dx=0, dy=0):
    d, width = glyphs()
    tr = ' transform="translate(%s %s)"' % (f(dx), f(dy)) if (dx or dy) else ""
    return ('<path%s d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round"/>'
            % (tr, d, color, SW)), width


# ---- files
for name, bf, sf, small in (("symbol-black.svg", "#000", "#000", False), ("symbol-small-black.svg", "#000", "#000", True),
                            ("symbol-on-light.svg", INK, BLUE_DEEP, False), ("symbol-on-dark.svg", LIGHT, BLUE, False),
                            ("symbol-white.svg", "#fff", "#fff", False), ("symbol-small-on-dark.svg", LIGHT, BLUE, True)):
    write(name, svg("0 0 256 256", mark_group(bf, sf, small, 0, 0 if small else 8)))  # optical centre

# app icon: dark tile, light body, blue straw (the screen and stick holes show the tile)
write("app-icon.svg", svg("0 0 256 256", '<rect width="256" height="256" rx="56" fill="%s"/>' % TILE
                          + mark_group(LIGHT, BLUE, False, 128 - 128 * 0.82, 128 - 128 * 0.82 - 4, 0.82)))
write("app-icon-small.svg", svg("0 0 256 256", '<rect width="256" height="256" rx="48" fill="%s"/>' % TILE
                                + mark_group(LIGHT, BLUE, True, 128 - 128 * 0.88, 128 - 128 * 0.88 - 6, 0.88)))

wm_black, wm_w = wordmark("#000")
# horizontal lockup: mark at full height, wordmark baseline on the body's bottom third
gap = 28
W = 256 + gap + wm_w + 24
for name, bf, sf, tc in (("lockup-horizontal-on-light.svg", INK, BLUE_DEEP, INK), ("lockup-horizontal-on-dark.svg", LIGHT, BLUE, LIGHT),
                         ("lockup-horizontal-black.svg", "#000", "#000", "#000")):
    wm, _ = wordmark(tc, 256 + gap, 4)
    write(name, svg("0 0 %s 256" % f(W), mark_group(bf, sf) + wm))
# stacked lockup
SW_W = max(256, wm_w) + 48
for name, bf, sf, tc in (("lockup-stacked-on-light.svg", INK, BLUE_DEEP, INK), ("lockup-stacked-on-dark.svg", LIGHT, BLUE, LIGHT)):
    wm, _ = wordmark(tc, (SW_W - wm_w) / 2, 206)
    write(name, svg("0 0 %s 404" % f(SW_W), mark_group(bf, sf, False, (SW_W - 256 * 1.4) / 2, -30, 1.4) + wm))
print("wordmark width %.1f" % wm_w)
