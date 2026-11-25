"""Build the profile README and its SVG cards.

Reads projects.toml and scripts/README.template.md, draws the banner and one
card per project into assets/, and writes README.md. The list of public
repos that have no card is filled from the GitHub API. If that fetch fails,
the list keeps what README.md already had.

Text in the SVGs is drawn as glyph outlines from the fonts in fonts/, so the
cards look the same on every device (SVGs shown through <img> cannot load
web fonts).

    python scripts/build.py            # everything
    python scripts/build.py --offline  # skip the API, keep the old list
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tomllib
import urllib.request
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
USER = "ai-with-md"

# Light "lab notebook" palette: warm paper, deep indigo ink, four accents.
PAPER = "#FBF8F2"
EDGE = "#E6DFD2"
INK = "#262A40"
INK2 = "#4D4B5C"
MUTED = "#8A8696"
TRACK = "#ECE6DA"
BAR = "#CFC8BA"
GRID = "#EFE9DD"
ACCENTS = {
    "teal": "#0F7B72",
    "clay": "#C2542F",
    "ochre": "#A8701A",
    "indigo": "#4B47B8",
}


# ---------------------------------------------------------------- text ----

class Face:
    """One font file, drawn as reusable glyph outlines."""

    def __init__(self, key: str, file: str):
        self.key = key
        self.font = TTFont(ROOT / "fonts" / file)
        self.cmap = self.font.getBestCmap()
        self.glyphs = self.font.getGlyphSet()
        self.hmtx = self.font["hmtx"]
        self.upm = self.font["head"].unitsPerEm

    def name(self, ch: str) -> str:
        g = self.cmap.get(ord(ch))
        if g is None:
            raise ValueError(f"{self.key} has no glyph for {ch!r}")
        return g

    def width(self, text: str, size: float, track: float = 0.0) -> float:
        units = sum(self.hmtx[self.name(c)][0] for c in text)
        return units * size / self.upm + track * max(len(text) - 1, 0)

    def wrap(self, text: str, size: float, max_width: float) -> list[str]:
        lines, line = [], ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if line and self.width(trial, size) > max_width:
                lines.append(line)
                line = word
            else:
                line = trial
        return lines + [line] if line else lines


FACES = {
    "serif": Face("fr", "Fraunces-Regular.ttf"),
    "serif-bold": Face("fb", "Fraunces-SemiBold.ttf"),
    "sans": Face("ir", "Inter-Regular.ttf"),
    "sans-bold": Face("ib", "Inter-SemiBold.ttf"),
    "mono": Face("mr", "PlexMono-Regular.ttf"),
    "mono-bold": Face("mm", "PlexMono-Medium.ttf"),
}


def tidy(text: str) -> str:
    """Typographic apostrophes, and no em dashes anywhere."""
    if "\u2014" in text:
        raise ValueError(f"em dash in: {text}")
    return text.replace("'", "’")


class Svg:
    def __init__(self, width: int, height: int, title: str, desc: str):
        self.w, self.h = width, height
        self.title, self.desc = title, desc
        self.defs: dict[str, str] = {}
        self.body: list[str] = []

    def add(self, element: str) -> None:
        self.body.append(element)

    def text(self, text, x, y, face="sans", size=14.0, fill=INK,
             track=0.0, anchor="start") -> float:
        """Draw one line; returns its width. anchor is start or end."""
        f = FACES[face]
        text = tidy(text)
        width = f.width(text, size, track)
        if anchor == "end":
            x -= width
        scale = size / f.upm
        parts = []
        for ch in text:
            g = f.name(ch)
            gid = f"{f.key}-{f.font.getGlyphID(g)}"
            if gid not in self.defs:
                pen = SVGPathPen(f.glyphs, ntos=lambda v: f"{v:.0f}")
                f.glyphs[g].draw(pen)
                self.defs[gid] = pen.getCommands()
            if self.defs[gid]:
                parts.append(f'<use href="#{gid}" transform="translate({x:.2f} {y:.2f}) '
                             f'scale({scale:.5f} {-scale:.5f})"/>')
            x += f.hmtx[g][0] * scale + track
        self.add(f'<g fill="{fill}">{"".join(parts)}</g>')
        return width

    def render(self) -> str:
        defs = "".join(f'<path id="{k}" d="{d}"/>' for k, d in self.defs.items() if d)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'width="{self.w}" height="{self.h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{html.escape(self.title)}</title>'
            f'<desc id="d">{html.escape(self.desc)}</desc>'
            f"<defs>{defs}</defs>{''.join(self.body)}</svg>\n"
        )


def frame(svg: Svg) -> None:
    svg.add(f'<rect x="0.5" y="0.5" width="{svg.w - 1}" height="{svg.h - 1}" rx="14" '
            f'fill="{PAPER}" stroke="{EDGE}"/>')


def label(svg: Svg, x: float, y: float, words: list[str], fill: str, size: float = 12) -> None:
    svg.text("  ·  ".join(w.upper() for w in words), x, y, "mono-bold", size, fill, track=1.1)


# ---------------------------------------------------------------- cards ---

def bars(svg: Svg, p: dict, x: float, y: float, width: float, accent: str, k: float = 1.0) -> float:
    """Labelled horizontal bars, sized by k; returns the y below the last one."""
    top = max(b["value"] for b in p["bars"])
    value_room = max(FACES["mono-bold"].width(b["text"], 13 * k) for b in p["bars"]) + 12 * k
    room = width - value_room
    for b in p["bars"]:
        svg.text(b["label"].upper(), x, y, "mono", 12 * k, MUTED, track=0.5)
        length = max(room * b["value"] / top, 3)
        colour = accent if b.get("highlight") else BAR
        by, bh = y + 9 * k, 13 * k
        svg.add(f'<rect x="{x}" y="{by:.1f}" width="{room:.1f}" height="{bh:.1f}" rx="2" fill="{TRACK}"/>')
        svg.add(f'<rect x="{x}" y="{by:.1f}" width="{length:.1f}" height="{bh:.1f}" rx="2" fill="{colour}"/>')
        svg.text(b["text"], x + room + 12 * k, by + bh - 1.5 * k, "mono-bold", 13 * k,
                 INK if b.get("highlight") else INK2)
        y += 52 * k
    return y


def card(p: dict) -> str:
    """Grid card, 600 by 340."""
    accent = ACCENTS[p["accent"]]
    svg = Svg(600, 340, p["question"], p["alt"])
    frame(svg)
    label(svg, 30, 44, [p["kind"], p["area"]], accent, 12.5)
    svg.text(p["repo"], 570, 44, "mono", 12.5, MUTED, anchor="end")

    # Shrink a long question to fit one line, down to 23px, then wrap.
    size = 28.0
    while size > 23 and FACES["serif-bold"].width(tidy(p["question"]), size) > 540:
        size -= 0.5
    q = FACES["serif-bold"].wrap(tidy(p["question"]), size, 540)
    y = 90
    for line in q[:2]:
        svg.text(line, 30, y, "serif-bold", size, INK)
        y += 34
    top = y - 6

    svg.text(p["big"], 28, top + 66, "serif", 74, INK)
    cap = FACES["sans"].wrap(tidy(p.get("caption_short", p["caption"])), 15.5, 262)
    cy = top + 98
    for line in cap[:4]:
        svg.text(line, 30, cy, "sans", 15.5, INK2)
        cy += 21
    if len(cap) > 4:
        raise ValueError(f"caption too long on {p['repo']}: add a shorter caption_short")

    bars(svg, p, 322, top + 22, 248, accent)
    svg.text(p["cta"] + "  →", 30, 318, "sans-bold", 15.5, accent)
    if cy > 302:
        raise ValueError(f"card overflows on {p['repo']}")
    return svg.render()


def wide_card(p: dict) -> str:
    """Wide card for the newest project, 1200 by 320."""
    accent = ACCENTS[p["accent"]]
    svg = Svg(1200, 320, p["question"], p["alt"])
    frame(svg)
    svg.add(f'<rect x="40" y="30" width="68" height="26" rx="13" fill="{accent}"/>')
    svg.text("NEW", 55, 48.5, "mono-bold", 13, PAPER, track=1.6)
    label(svg, 124, 49, [p["kind"], p["area"]], accent, 13.5)
    svg.text(p["repo"], 1160, 49, "mono", 13.5, MUTED, anchor="end")

    svg.text(p["question"], 40, 112, "serif-bold", 40, INK)
    svg.text(p["big"], 36, 196, "serif", 80, INK)
    cap = FACES["sans"].wrap(tidy(p["caption"]), 17, 560)
    y = 230
    for line in cap[:3]:
        svg.text(line, 40, y, "sans", 17, INK2)
        y += 23
    if len(cap) > 3:
        raise ValueError(f"caption too long on {p['repo']}")

    bars(svg, p, 660, 132, 500, accent, k=1.08 if len(p["bars"]) < 3 else 0.96)
    svg.text(p["cta"] + "  →", 1160, 292, "sans-bold", 17, accent, anchor="end")
    return svg.render()


def banner() -> str:
    """Name, headline, and a sketch of an estimate narrowing until it clears the bar."""
    svg = Svg(1200, 300, "Meenal Asesh: I help teams decide what to ship with AI",
              "AI strategy and data science, 12+ years. Which model, how many evals, "
              "what it costs, and whether it got worse. A sketch shows an estimate's "
              "interval narrowing as data arrives until it clears the worth-it line.")
    frame(svg)
    for gx in range(700, 1180, 24):
        svg.add(f'<line x1="{gx}" y1="24" x2="{gx}" y2="276" stroke="{GRID}"/>')
    for gy in range(36, 276, 24):
        svg.add(f'<line x1="688" y1="{gy}" x2="1176" y2="{gy}" stroke="{GRID}"/>')

    label(svg, 48, 64, ["Meenal Asesh", "AI strategy & data science", "12+ years"], ACCENTS["clay"], 13)
    x = 46 + svg.text("I help teams decide", 46, 128, "serif", 50, INK)
    x = 46 + svg.text("what to ", 46, 186, "serif", 50, INK)
    x += svg.text("ship", x, 186, "serif-bold", 50, ACCENTS["clay"])
    svg.text(" with AI.", x, 186, "serif", 50, INK)
    svg.text("Which model. How many evals. What it costs. Whether it got worse.",
             48, 240, "sans", 18, INK2)

    # The sketch: x is the effect of a change; each row has more data.
    zero, worth = 806, 922
    svg.add(f'<line x1="{zero}" y1="70" x2="{zero}" y2="250" stroke="{MUTED}" stroke-dasharray="3 4"/>')
    svg.add(f'<line x1="{worth}" y1="70" x2="{worth}" y2="250" stroke="{ACCENTS["teal"]}" '
            f'stroke-width="1.5" stroke-dasharray="6 4"/>')
    svg.text("NO CHANGE", zero - 6, 62, "mono", 12, MUTED, track=0.8, anchor="end")
    svg.text("WORTH IT", worth + 6, 62, "mono-bold", 12, ACCENTS["teal"], track=0.8)
    rows = [("n = 50", 990, 300), ("n = 200", 985, 150), ("n = 800", 1000, 78), ("n = 3,200", 1004, 40)]
    for i, (n, mid, half) in enumerate(rows):
        y = 100 + i * 42
        lo, hi = max(mid - half, 712), min(mid + half, 1168)
        done = i == len(rows) - 1
        colour = ACCENTS["teal"] if done else BAR
        svg.add(f'<line x1="{lo}" y1="{y}" x2="{hi}" y2="{y}" stroke="{colour}" stroke-width="4" '
                f'stroke-linecap="round"/>')
        svg.add(f'<circle cx="{mid}" cy="{y}" r="6" fill="{PAPER}" stroke="{INK if done else MUTED}" '
                f'stroke-width="2"/>')
        svg.text(n, 712, y - 10, "mono", 12, MUTED)
    svg.text("SHIP IT", 1168, 256, "mono-bold", 13, ACCENTS["teal"], track=1.2, anchor="end")
    return svg.render()


# ---------------------------------------------------------------- web -----

def fetch(url: str) -> bytes:
    headers = {"User-Agent": "profile-readme-builder"}
    if url.startswith("https://api.github.com") and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
        return r.read()


def more(carded: set[str]) -> str:
    """Public repos without a card, newest first. Never lists private repos."""
    repos = json.loads(fetch(f"https://api.github.com/users/{USER}/repos?per_page=100&sort=created"))
    rows = []
    for r in repos:
        if r["private"] or r["fork"] or r["archived"] or r["name"] in carded or r["name"] == USER:
            continue
        desc = tidy((r["description"] or "").strip())
        rows.append(f"- [**{r['name']}**]({r['html_url']}) {desc}")
    return "\n".join(rows) if rows else "- More soon."


def keep(readme: str, name: str) -> str:
    m = re.search(rf"<!-- {name} starts -->\n(.*?)\n<!-- {name} ends -->", readme, re.S)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- main ----

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    projects = tomllib.loads((ROOT / "projects.toml").read_text())["project"]
    projects.sort(key=lambda p: p["date"], reverse=True)
    newest, rest = projects[0], projects[1:]
    grid = rest[: len(rest) // 2 * 2]
    overflow = rest[len(grid):]

    cards_dir = ROOT / "assets" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for old in cards_dir.glob("*.svg"):
        old.unlink()
    (ROOT / "assets" / "banner.svg").write_text(banner())
    (cards_dir / f"{newest['repo']}.svg").write_text(wide_card(newest))
    for p in grid:
        (cards_dir / f"{p['repo']}.svg").write_text(card(p))

    def img(p, width):
        return (f'<a href="{p["link"]}"><img src="assets/cards/{p["repo"]}.svg" '
                f'width="{width}" alt="{html.escape(tidy(p["alt"]), quote=True)}"></a>')

    rows = [f"<p>\n{img(newest, '100%')}\n</p>"]
    for a, b in zip(grid[::2], grid[1::2]):
        rows.append(f"<p>\n{img(a, '49%')}\n{img(b, '49%')}\n</p>")

    readme_path = ROOT / "README.md"
    current = readme_path.read_text() if readme_path.exists() else ""
    sections = {"more": keep(current, "more")}
    if not args.offline:
        carded = {p["repo"] for p in [newest, *grid]}
        for name, build in (("more", lambda: more(carded)),):
            try:
                sections[name] = build()
            except Exception as exc:  # keep the old section rather than fail the build
                print(f"kept the old {name} section: {exc}")
    for p in overflow:  # a card that just left the grid stays listed, even offline
        if f"[**{p['repo']}**]" not in sections["more"]:
            sections["more"] = f"- [**{p['repo']}**]({p['link']}) {tidy(p['question'])}\n" + sections["more"]

    out = (ROOT / "scripts" / "README.template.md").read_text()
    out = out.replace("{{ cards }}", "\n".join(rows))
    for name, body in sections.items():
        out = out.replace("{{ %s }}" % name, f"<!-- {name} starts -->\n{body}\n<!-- {name} ends -->")
    if "\u2014" in out:
        raise ValueError("em dash in README")
    readme_path.write_text(out)
    print(f"built {1 + len(grid)} cards, {len(overflow)} in the text list")


if __name__ == "__main__":
    main()
