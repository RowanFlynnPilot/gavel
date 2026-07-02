"""Render a shareable "this week's meetings" PNG for the newsletter.

    python -m engine.digest [--days N] [--out PATH] [--date YYYY-MM-DD]

Port of marathon-meetings generate_digest.py with every brand value driven
by instance.json: header band = theme.primary_dark, footer accent =
theme.digest_accent (falls back to primary_dark), wordmark = instance.name,
footer domain = newsroom_url host, per-jurisdiction labels/accents/avatars
from the jurisdictions list, fonts from theme.font_files.

Data source: data/upcoming.json. Pure Pillow — text is supersampled 2x and
downscaled with LANCZOS for crisp edges.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import UPCOMING_JSON, InstanceConfig, load_instance, setup_logging
from .errors import ConfigError

SCALE = 2      # supersample factor for crisp text
WIDTH = 840    # final width (px)
MARGIN = 40

# Neutral grays — not brand tokens, shared across instances.
WHITE = (255, 255, 255)
MUTED = (122, 112, 96)
NAME_INK = (38, 30, 20)


def _rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class Theme:
    """Digest palette + fonts resolved from instance.json."""

    def __init__(self, cfg: InstanceConfig):
        root = cfg.path.parent
        t = cfg.theme
        self.ink = _rgb(t["ink"])
        self.band = _rgb(t["primary_dark"])
        self.divider = _rgb(t["divider"])
        self.accent = _rgb(t.get("digest_accent", t["primary_dark"]))
        self.logo = root / t["logo"]

        files = t.get("font_files")
        if not files or "display" not in files or "body" not in files:
            raise ConfigError(
                "instance.json: theme.font_files must define at least "
                "'display' and 'body' font paths for the digest")
        self._display = root / files["display"]
        self._body = root / files["body"]
        for p in (self._display, self._body):
            if not p.exists():
                raise ConfigError(f"digest font not found: {p}")

        # jurisdiction key → (label, accent rgb, avatar path)
        self.sources = {
            j.key: (j.name, _rgb(j.accent), root / j.avatar)
            for j in cfg.jurisdictions
        }
        self.wordmark = cfg.name.upper()
        self.domain = (cfg.newsroom_url.split("//", 1)[-1]
                       .removeprefix("www.").strip("/").upper())
        self.tagline = cfg.name

    def display(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self._display), size * SCALE)

    def body(self, size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
        fnt = ImageFont.truetype(str(self._body), size * SCALE)
        try:
            fnt.set_variation_by_axes([weight])
        except Exception:
            pass
        return fnt


# ── Drawing helpers ───────────────────────────────────────────────────────────

def _tracked_text(draw, xy, text, font, fill, tracking=0):
    """Draw text with letter-spacing (tracking in final px). Returns end x (1x)."""
    x, y = xy[0] * SCALE, xy[1] * SCALE
    tr = tracking * SCALE
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tr
    return x / SCALE


def _text_w(draw, text, font, tracking=0):
    w = sum(draw.textlength(ch, font=font) for ch in text) \
        + tracking * SCALE * max(len(text) - 1, 0)
    return w / SCALE


def _truncate(draw, text, font, max_w):
    if _text_w(draw, text, font) <= max_w:
        return text
    ell = "…"
    while text and _text_w(draw, text + ell, font) > max_w:
        text = text[:-1]
    return (text.rstrip() + ell) if text else ell


def _wrap_lines(draw, text, font, max_w, max_lines=2):
    """Greedy word-wrap into up to max_lines that each fit max_w. If the text
    still overflows, the last line is ellipsized."""
    words = text.split()
    lines, cur, wi = [], "", 0
    while wi < len(words) and len(lines) < max_lines:
        w = words[wi]
        trial = (cur + " " + w).strip()
        if not cur or _text_w(draw, trial, font) <= max_w:
            cur, wi = trial, wi + 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < max_lines:
        lines.append(cur)
        cur = ""
    if wi < len(words):
        last = lines[-1] if lines else ""
        rest = (last + " " + " ".join(words[wi:])).strip()
        lines[-1:] = [_truncate(draw, rest, font, max_w)]
    return lines or [""]


def _circle_avatar(path, d):
    """Return a circular RGBA avatar of diameter d (1x)."""
    dd = d * SCALE
    try:
        img = Image.open(path).convert("RGBA").resize((dd, dd), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (dd, dd), (230, 226, 218, 255))
    mask = Image.new("L", (dd, dd), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, dd - 1, dd - 1), fill=255)
    img.putalpha(mask)
    return img


# ── Data ──────────────────────────────────────────────────────────────────────

def _time_key(t):
    try:
        return datetime.strptime(t, "%I:%M %p").time()
    except ValueError:
        return datetime.strptime("11:59 PM", "%I:%M %p").time()


def load_week(theme: Theme, today, days=7):
    """Return ordered list of (date, [events]) for days with meetings."""
    data = json.loads(UPCOMING_JSON.read_text(encoding="utf-8"))
    end = today + timedelta(days=days)
    by_day = {}
    for src, evs in data.items():
        if src not in theme.sources:
            continue
        for e in evs:
            try:
                d = datetime.strptime(e.get("date", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (today <= d <= end):
                continue
            name = (e.get("name") or "").strip()
            if "CANCEL" in name.upper():
                continue
            by_day.setdefault(d, []).append({
                "source": src, "time": e.get("time", "").strip(), "name": name,
            })
    out = []
    for d in sorted(by_day):
        evs = sorted(by_day[d], key=lambda x: _time_key(x["time"]))
        out.append((d, evs))
    return out


# ── Render ────────────────────────────────────────────────────────────────────

def render(cfg: InstanceConfig, today, days=7, out="digest.png"):
    theme = Theme(cfg)
    week = load_week(theme, today, days)

    HEADER_H = 74
    SECTION_H = 88
    DAY_H = 40
    FOOTER_H = 62
    GROUP_GAP = 10
    ROW_TOP = 26
    LINE_H = 22
    ROW_BOT = 10

    # Pre-pass: wrap each meeting name to <=2 lines & size its row.
    _mdraw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    _nfont = theme.body(18, 500)
    _tfont = theme.display(19)
    _text_x = MARGIN + 38 + 14
    for _d, evs in week:
        for ev in evs:
            ev["_time_txt"] = ev["time"] or "TIME TBD"
            ev["_time_w"] = _text_w(_mdraw, ev["_time_txt"], _tfont, tracking=1)
            name_max = (WIDTH - MARGIN - ev["_time_w"] - 18) - _text_x
            ev["_lines"] = _wrap_lines(_mdraw, ev["name"], _nfont, name_max, max_lines=2)
            ev["_row_h"] = ROW_TOP + len(ev["_lines"]) * LINE_H + ROW_BOT

    body_h = 0
    for _d, evs in week:
        body_h += DAY_H + sum(ev["_row_h"] for ev in evs) + GROUP_GAP
    if not week:
        body_h = 90
    height = HEADER_H + SECTION_H + body_h + FOOTER_H + 18

    W, H = WIDTH * SCALE, height * SCALE
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    def rect(x0, y0, x1, y1, fill):
        d.rectangle([x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE], fill=fill)

    # Header band (primary_dark, full-width).
    rect(0, 0, WIDTH, HEADER_H, theme.band)

    band_cy = HEADER_H / 2
    logo_d = 46
    logo = _circle_avatar(theme.logo, logo_d)
    img.paste(logo, (int(MARGIN * SCALE), int((band_cy - logo_d / 2) * SCALE)), logo)

    tf = theme.display(30)
    tw = _text_w(d, theme.wordmark, tf, tracking=3)
    _tracked_text(d, ((WIDTH - tw) / 2, band_cy - 16), theme.wordmark, tf,
                  WHITE, tracking=3)

    # Section header (centered): title + date range.
    start_lbl = today.strftime("%b %d").replace(" 0", " ")
    end_d = week[-1][0] if week else today + timedelta(days=days)
    end_lbl = end_d.strftime("%b %d").replace(" 0", " ")

    t1 = "MEETINGS THIS WEEK"
    f1 = theme.display(32)
    w1 = _text_w(d, t1, f1, tracking=6)
    _tracked_text(d, ((WIDTH - w1) / 2, HEADER_H + 24), t1, f1, theme.ink, tracking=6)

    t2 = f"{start_lbl.upper()}  –  {end_lbl.upper()}"
    f2 = theme.display(15)
    w2 = _text_w(d, t2, f2, tracking=3)
    _tracked_text(d, ((WIDTH - w2) / 2, HEADER_H + 64), t2, f2, theme.band, tracking=3)

    y = HEADER_H + SECTION_H

    if not week:
        d.text((MARGIN * SCALE, y * SCALE),
               "No public meetings scheduled in the week ahead.",
               font=theme.body(18, 400), fill=MUTED)

    for gi, (day, evs) in enumerate(week):
        if gi > 0:
            rect(MARGIN, y + 2, WIDTH - MARGIN, y + 2 + 1, theme.divider)
        day_lbl = day.strftime("%A  ·  %B %d").replace(" 0", " ").upper()
        _tracked_text(d, (MARGIN, y + 14), day_lbl, theme.display(16),
                      theme.ink, tracking=1.5)
        y += DAY_H

        for ev in evs:
            label, accent, avatar_path = theme.sources[ev["source"]]
            row_h = ev["_row_h"]
            row_cy = y + row_h / 2

            av_d = 38
            av = _circle_avatar(avatar_path, av_d)
            img.paste(av, (int(MARGIN * SCALE), int((row_cy - av_d / 2) * SCALE)), av)

            text_x = MARGIN + av_d + 14

            tfont = theme.display(19)
            _tracked_text(d, (WIDTH - MARGIN - ev["_time_w"], row_cy - 11),
                          ev["_time_txt"], tfont, theme.band, tracking=1)

            _tracked_text(d, (text_x, y + 9), label.upper(), theme.display(12),
                          accent, tracking=1.2)

            nfont = theme.body(18, 500)
            for i, line in enumerate(ev["_lines"]):
                d.text((text_x * SCALE, (y + ROW_TOP + i * LINE_H) * SCALE),
                       line, font=nfont, fill=NAME_INK)

            y += row_h
        y += GROUP_GAP

    # Footer.
    fy = height - FOOTER_H
    rect(MARGIN, fy, WIDTH - MARGIN, fy + 1, theme.divider)
    _tracked_text(d, (MARGIN, fy + 16), theme.domain, theme.display(14),
                  theme.accent, tracking=1.5)
    d.text((MARGIN * SCALE, (fy + 34) * SCALE), theme.tagline,
           font=theme.body(12, 400), fill=MUTED)
    upd = f"UPDATED {today.strftime('%b %d').replace(' 0', ' ').upper()}"
    uw = _text_w(d, upd, theme.display(13), tracking=1)
    _tracked_text(d, (WIDTH - MARGIN - uw, fy + 17), upd, theme.display(13),
                  MUTED, tracking=1)

    # Subtle outer card border, then downscale for crisp output.
    d.rectangle([0, 0, W - SCALE, H - SCALE], outline=theme.divider, width=SCALE)
    final = img.resize((WIDTH, height), Image.LANCZOS)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    final.save(out, "PNG")
    print(f"[digest] wrote {out}  ({WIDTH}x{height})  "
          f"{sum(len(e) for _, e in week)} meetings / {len(week)} day(s)")
    return out


def main():
    setup_logging()
    ap = argparse.ArgumentParser(description="Render the weekly meetings digest PNG.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="digest.png")
    ap.add_argument("--date", default=None, help="Override today (YYYY-MM-DD) for testing")
    args = ap.parse_args()
    today = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    render(load_instance(), today, days=args.days, out=args.out)


if __name__ == "__main__":
    main()
