"""Builds the README's hero, download buttons and flow diagram in docs/assets/.

Run `python3 docs/assets/build_readme_art.py` after changing this file; edit the script, not the SVGs.
Colours come from design/logo/README.md. Shapes reuse the logo's monoline language: round-capped strokes,
with straw blue marking the path an update takes.
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent.parent / "design" / "logo" / "kit"
FONT = "system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans',sans-serif"

THEMES = {
    "dark": {"node": "#1a1d23", "title": "#e6e8eb", "sub": "#aab2bd", "flow": "#1a9fff", "symbol": "symbol-on-dark.svg"},
    "light": {"node": "#eef1f4", "title": "#0f1115", "sub": "#4a5260", "flow": "#1475c4", "symbol": "symbol-on-light.svg"},
}
BUTTON_FILL = "#1475c4"  # the app's primary button; white text on it is 4.8:1 on both page themes


def symbol(theme, x, y, size):
    """The logo mark from the kit, placed at x, y and scaled to size px."""
    svg = (KIT / THEMES[theme]["symbol"]).read_text()
    paths = "".join(re.findall(r"<path[^>]*/>", svg))
    return f'<g transform="translate({x} {y}) scale({size / 256})">{paths}</g>'


def text(x, y, s, size, fill, weight=400, anchor="start"):
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}">{s}</text>')


def svg(w, h, title, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img"><title>{title}</title>{body}</svg>\n')


# ---------------------------------------------------------------- download buttons

GLYPH = 'fill="none" stroke="#fff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"'
DESKTOP_GLYPH = (f'<g {GLYPH}><rect x="4" y="6" width="32" height="22" rx="3"/>'
                 f'<path d="M20 28V34M12 34H28"/></g>')
HANDHELD_GLYPH = (f'<g {GLYPH}><rect x="2" y="10" width="36" height="20" rx="9"/>'
                  f'<rect x="13" y="14" width="14" height="12" rx="2"/></g>')


def button(title, glyph, line1, line2):
    body = (f'<rect width="400" height="96" rx="10" fill="{BUTTON_FILL}"/>'
            f'<g transform="translate(24 28)">{glyph}</g>'
            + text(84, 42, line1, 20, "#fff", 600)
            + text(84, 68, line2, 15, "#fff"))
    return svg(400, 96, title, body)


# ---------------------------------------------------------------- flow diagram

NODES = [
    ("A new release", ["on GitHub, GitLab, Codeberg,", "Gitea or Forgejo"]),
    ("SedotDeck", ["picks the Linux file and", "backs up your saves"]),
    ("Your Steam library", ["same shortcut,", "new version"]),
]


def node(theme, x, y, w, h, i):
    t = THEMES[theme]
    title, subs = NODES[i]
    cx = x + w / 2
    out = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{t["node"]}"/>'
    text_h = 65  # title cap height to the last line's descenders
    if i == 1:  # the mark makes SedotDeck the one focal point
        top = y + (h - (52 + 14 + text_h)) / 2
        out += symbol(theme, cx - 26, top - 8, 52)  # the mark's art starts 8 units down its 256 box
        ty = top + 52 + 14 + 15
    else:
        ty = y + (h - text_h) / 2 + 15
    out += text(cx, ty, title, 19, t["title"], 600, "middle")
    for k, s in enumerate(subs):
        out += text(cx, ty + 26 + k * 20, s, 15, t["sub"], 400, "middle")
    return out


def connector(theme, x1, y1, x2, y2):
    """A straw-blue stroke with a chevron at the end: the direction an update travels."""
    c = THEMES[theme]["flow"]
    s = f'fill="none" stroke="{c}" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"'
    if y1 == y2:
        head = f"M{x2 - 9} {y2 - 9}L{x2} {y2}L{x2 - 9} {y2 + 9}"
    else:
        head = f"M{x2 - 9} {y2 - 9}L{x2} {y2}L{x2 + 9} {y2 - 9}"
    return f'<path {s} d="M{x1} {y1}L{x2} {y2}{head}"/>'


FLOW_TITLE = ("A new release on GitHub, GitLab, Codeberg, Gitea or Forgejo goes through SedotDeck, which picks the "
              "Linux file and backs up your saves, into your Steam library: same shortcut, new version.")


def flow_wide(theme):
    w, h, nw = 880, 160, 256
    body = ""
    for i in range(3):
        body += node(theme, i * 312, 0, nw, h, i)
    for i in range(2):
        x = i * 312 + nw
        body += connector(theme, x + 12, h / 2, x + 44, h / 2)
    return svg(w, h, FLOW_TITLE, body)


def flow_stacked(theme):
    nw, nh, gap = 320, 150, 52
    body = ""
    for i in range(3):
        body += node(theme, 0, i * (nh + gap), nw, nh, i)
    for i in range(2):
        y = i * (nh + gap) + nh
        body += connector(theme, nw / 2, y + 12, nw / 2, y + gap - 12)
    return svg(nw, 3 * nh + 2 * gap, FLOW_TITLE, body)


# ---------------------------------------------------------------- hero

SHOTS = HERE.parent / "screenshots"
HERO_TITLE = ("SedotDeck. Add it once. It stays up to date. Obtainium for Steam Deck: recomps, PC ports and emulators, "
              "updated and kept in your Steam library. 7 catalogs to browse and add from, 5 code hosts (GitHub, "
              "GitLab and more), no sudo: one Python file. Screenshots of My apps and Browse.")
# Facts from the README's own feature list; nothing here is a measurement.
FACTS = [("7 catalogs", "to browse and add"), ("5 code hosts", "GitHub, GitLab and more"),
         ("No sudo", "one Python file")]


def lockup(theme, x, y, height):
    svg_text = (KIT / f"lockup-horizontal-on-{theme}.svg").read_text()
    inner = re.sub(r"^<svg[^>]*>|</svg>\s*$|<title>.*?</title>", "", svg_text.strip())
    return f'<g transform="translate({x} {y}) scale({height / 256})">{inner}</g>'


def shot(name, x, y, w, h, ring):
    """A real screenshot, embedded (GitHub won't let an SVG image load other files), with corners clipped.
    The ring in the page colour separates it from a screenshot behind it without a shadow."""
    import base64
    data = base64.b64encode((SHOTS / f"{name}.webp").read_bytes()).decode()
    cid = f"clip-{name}"
    return (f'<clipPath id="{cid}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14"/></clipPath>'
            f'<rect x="{x - 8}" y="{y - 8}" width="{w + 16}" height="{h + 16}" rx="20" fill="{ring}"/>'
            f'<image href="data:image/webp;base64,{data}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'clip-path="url(#{cid})" preserveAspectRatio="xMidYMid slice"/>')


def headline(theme, x, y, size, gap):
    """Three lines; straw blue lands on the promise, the one accent in the hero."""
    t = THEMES[theme]
    style = f'font-family="{FONT}" font-size="{size}" font-weight="800" letter-spacing="-2"'
    lines = [("Add it once.", t["title"]), ("It stays", t["title"]), ("up to date.", t["flow"])]
    return "".join(f'<text x="{x}" y="{y + i * gap}" {style} fill="{c}">{s}</text>' for i, (s, c) in enumerate(lines))


def facts(theme, x, y, col, title_size, sub_size):
    t = THEMES[theme]
    out = ""
    for i, (a, b) in enumerate(FACTS):
        out += text(x + i * col, y, a, title_size, t["title"], 700)
        out += text(x + i * col, y + sub_size + 14, b, sub_size, t["sub"])
    return out


def hero_wide(theme):
    t = THEMES[theme]
    w, h = 1600, 780
    body = f'<rect width="{w}" height="{h}" rx="28" fill="{t["node"]}"/>'
    body += lockup(theme, 80, 56, 76)
    body += headline(theme, 84, 290, 92, 96)
    body += text(88, 560, "Obtainium for Steam Deck: recomps, PC ports and", 28, t["sub"])
    body += text(88, 600, "emulators, updated and kept in your Steam library.", 28, t["sub"])
    body += facts(theme, 88, 690, 276, 36, 22)
    body += shot("2-browse", 920, 64, 608, 380, t["node"])
    body += shot("1-my-apps", 856, 318, 608, 380, t["node"])
    body += text(1464, 740, "SedotDeck 0.1.0 at 1280×800", 20, t["sub"], 400, "end")
    return svg(w, h, HERO_TITLE, body)


def hero_phone(theme):
    t = THEMES[theme]
    w, h = 800, 1425
    body = f'<rect width="{w}" height="{h}" rx="28" fill="{t["node"]}"/>'
    body += lockup(theme, 52, 48, 84)
    body += headline(theme, 56, 290, 96, 100)
    body += text(60, 560, "Obtainium for Steam Deck: recomps,", 34, t["sub"])
    body += text(60, 606, "PC ports and emulators, updated and", 34, t["sub"])
    body += text(60, 652, "kept in your Steam library.", 34, t["sub"])
    for i, (a, b) in enumerate(FACTS):  # one per row: three columns don't fit a phone
        body += text(60, 740 + i * 62, a, 40, t["title"], 700)
        body += text(340, 740 + i * 62, b, 28, t["sub"])
    body += shot("1-my-apps", 60, 940, 680, 425, t["node"])
    return svg(w, h, HERO_TITLE, body)


def main():
    files = {
        "get-appimage.svg": button("Download the AppImage for Desktop Mode", DESKTOP_GLYPH,
                                   "Download the AppImage", "Desktop Mode, one file"),
        "get-decky.svg": button("Get the Decky plugin for Game Mode", HANDHELD_GLYPH,
                                "Get the Decky plugin", "Game Mode, install from ZIP"),
    }
    for theme in THEMES:
        files[f"flow-{theme}.svg"] = flow_wide(theme)
        files[f"flow-phone-{theme}.svg"] = flow_stacked(theme)
        files[f"hero-{theme}.svg"] = hero_wide(theme)
        files[f"hero-phone-{theme}.svg"] = hero_phone(theme)
    for name, content in files.items():
        (HERE / name).write_text(content)
        print("wrote", name)


if __name__ == "__main__":
    main()
