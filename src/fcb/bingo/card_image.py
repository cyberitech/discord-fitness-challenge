"""Render fitness-bingo cards as PNGs (Pillow).

Each of the six cards renders as its OWN large image so the task text in every square is
easily readable; the bot posts all six as separate attachments in one message. Crossed
squares are filled green with a check; the single highest-value completed pattern on the
card is ringed in gold.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from fcb.bingo.scoring import GRID_SIZE, PATTERN_LABELS

logger = logging.getLogger(__name__)

# Palette
BG_TOP = (33, 28, 50)
BG_BOTTOM = (20, 17, 31)
PANEL = (44, 38, 68)
CELL_OPEN = (37, 31, 56)
CELL_OPEN_BORDER = (84, 73, 118)
CELL_DONE = (47, 120, 74)
CELL_DONE_BORDER = (96, 208, 138)
HILITE = (255, 209, 102)
ACCENT = (124, 92, 255)
TEXT = (255, 255, 255)
TEXT_ON_DONE = (236, 255, 243)
TASK_TEXT = (221, 213, 240)
SUBTLE = (201, 184, 255)
MUTED = (138, 127, 176)

# Single-card geometry (one 5x5 card per image, drawn large).
MARGIN = 26
TITLE_H = 70
CELL_W = 300
CELL_H = 172
CELL_GAP = 8
CELL_PAD = 14
LINE_H = 23
TASK_FONT = 19
GRID_W = GRID_SIZE * CELL_W + (GRID_SIZE - 1) * CELL_GAP
GRID_H = GRID_SIZE * CELL_H + (GRID_SIZE - 1) * CELL_GAP
CARD_PAD = 18
FOOTER_H = 40
WIDTH = MARGIN * 2 + GRID_W + CARD_PAD * 2
HEIGHT = MARGIN * 2 + TITLE_H + GRID_H + FOOTER_H + CARD_PAD * 2
MAX_LINES = (CELL_H - CELL_PAD * 2) // LINE_H

_FONT_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)
_FONT_REG = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False):
    for path in (_FONT_BOLD if bold else _FONT_REG):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_lerp(BG_TOP, BG_BOTTOM, y / max(1, h - 1)))
    return img


def _center(draw, cx, cy, text, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (box[2] - box[0]) / 2, cy - (box[3] - box[1]) / 2 - box[1]), text, font=font, fill=fill)


def _wrap(draw, text, font, max_w, max_lines):
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    consumed = " ".join(lines)
    if len(consumed) < len(text) and lines:
        last = lines[-1]
        while last and draw.textlength(last + "\u2026", font=font) > max_w:
            last = last[:-1]
        lines[-1] = (last + "\u2026") if last else "\u2026"
    return lines


def _check(draw, x, y, size):
    w = max(3, size // 9)
    draw.line(
        [(x + size * 0.2, y + size * 0.52), (x + size * 0.4, y + size * 0.72), (x + size * 0.8, y + size * 0.25)],
        fill=(215, 255, 228),
        width=w,
        joint="curve",
    )


def render_single_card_png(card: dict, event_name: str, member_name: str) -> bytes:
    """Render one 5x5 card to a large, readable PNG."""
    img = _gradient(WIDTH, HEIGHT)
    draw = ImageDraw.Draw(img)
    px0, py0 = MARGIN, MARGIN
    px1, py1 = WIDTH - MARGIN, HEIGHT - MARGIN
    draw.rounded_rectangle((px0, py0, px1, py1), radius=22, fill=PANEL)

    # Title block: big card label + small meta line.
    _center(draw, WIDTH // 2, py0 + 24, card["label"], _font(34, True), TEXT)
    _center(draw, WIDTH // 2, py0 + 52, f"{member_name}  \u00b7  {event_name}", _font(15), SUBTLE)

    gx = px0 + CARD_PAD
    gy = py0 + TITLE_H
    if not card.get("available", True):
        _center(draw, WIDTH // 2, gy + GRID_H // 2, "Not ready", _font(18), MUTED)
        return _encode(img)

    crossed = {c["index"] for c in card["cells"] if c["crossed"]}
    highlight = set(card.get("highlight", set()))
    by_index = {c["index"]: c for c in card["cells"]}
    task_font = _font(TASK_FONT)
    for index in range(GRID_SIZE * GRID_SIZE):
        r, c = divmod(index, GRID_SIZE)
        x = gx + c * (CELL_W + CELL_GAP)
        y = gy + r * (CELL_H + CELL_GAP)
        done = index in crossed
        draw.rounded_rectangle(
            (x, y, x + CELL_W, y + CELL_H),
            radius=12,
            fill=CELL_DONE if done else CELL_OPEN,
            outline=CELL_DONE_BORDER if done else CELL_OPEN_BORDER,
            width=2,
        )
        text = by_index.get(index, {}).get("text", "")
        lines = _wrap(draw, text, task_font, CELL_W - CELL_PAD * 2, MAX_LINES)
        ty = y + (CELL_H - len(lines) * LINE_H) / 2
        color = TEXT_ON_DONE if done else TASK_TEXT
        for line in lines:
            lw = draw.textlength(line, font=task_font)
            draw.text((x + (CELL_W - lw) / 2, ty), line, font=task_font, fill=color)
            ty += LINE_H
        if done:
            _check(draw, x + CELL_W - 30, y + 6, 22)
        if index in highlight:
            draw.rounded_rectangle((x - 2, y - 2, x + CELL_W + 2, y + CELL_H + 2), radius=13, outline=HILITE, width=4)

    n_crossed = sum(1 for c in card["cells"] if c["crossed"])
    bonus = card.get("bonus", 0)
    if card["pattern"]:
        left = f"{n_crossed} squares  +  {PATTERN_LABELS[card['pattern']]} bonus +{bonus}"
    else:
        left = f"{n_crossed} squares  \u00b7  no bonus yet"
    fy = gy + GRID_H + FOOTER_H // 2
    draw.text((px0 + CARD_PAD, fy - 2), left, font=_font(18, True), fill=SUBTLE)
    pts = f"{card['points']} pts"
    box = draw.textbbox((0, 0), pts, font=_font(20, True))
    draw.text((px1 - CARD_PAD - (box[2] - box[0]), fy - 3), pts, font=_font(20, True), fill=HILITE)
    return _encode(img)


def _encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_board_images(board_state: dict, member_name: str = "") -> list[tuple[str, bytes]]:
    """Render each available card to its own PNG. Returns [(filename, png_bytes), ...]
    in card order, suitable for attaching as separate files in one Discord message."""
    event_name = board_state.get("event_name") or "Fitness Bingo"
    images: list[tuple[str, bytes]] = []
    for card in board_state["cards"]:
        if not card.get("available", True):
            continue
        images.append((f"bingo-{card['card_key']}.png", render_single_card_png(card, event_name, member_name)))
    return images
