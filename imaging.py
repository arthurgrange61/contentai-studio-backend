"""
Composition d'images avec Pillow — texte overlay style TikTok.

Porté depuis le programme local de l'utilisateur (~/bijoux-tiktok), qui
produit un rendu bien plus soigné que l'ancien bandeau simple :
  - "bubble"  : une bulle par ligne, arrondie, couleur adaptée à la
                luminosité de la photo derrière (clair → bulle sombre,
                sombre → bulle blanche) — style TikTok natif.
  - "outline" : texte blanc gras avec contour noir épais, sans bulle,
                façon "story" Instagram/TikTok.

Sur Render (Linux), les polices système macOS de l'original n'existent pas :
la police embarquée (static/fonts/Poppins-Bold.ttf) est donc essayée en
premier, les polices macOS ne servant qu'en fallback pour un rendu identique
en local sur la machine de développement.
"""
import math
import os
import textwrap
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MAX_CHARS_PER_LINE = 20
TIKTOK_RATIO = 9 / 16

BUNDLED_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "Poppins-Bold.ttf")

FONT_CANDIDATES = [
    (BUNDLED_FONT, 0),
    ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
    ("/System/Library/Fonts/Helvetica.ttc", 1),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
    ("/System/Library/Fonts/SFNS.ttf", 0),
]


def _strip_emojis(text: str) -> str:
    """Retire les emojis/pictogrammes non affichables par les polices standard (évite les □)."""
    result = []
    for ch in text:
        code = ord(ch)
        if (0x1F000 <= code <= 0x1FAFF
            or 0x2600 <= code <= 0x27BF
            or 0x2190 <= code <= 0x21FF
            or 0xFE00 <= code <= 0xFE0F
            or code == 0x20E3
            or 0x1F1E6 <= code <= 0x1F1FF):
            continue
        result.append(ch)
    return " ".join("".join(result).split())


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path, index in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=index)
            except Exception:
                continue
    return ImageFont.load_default()


def crop_to_tiktok(image: Image.Image) -> Image.Image:
    """Recadre une image au format 9:16 TikTok en centrant."""
    w, h = image.size
    current_ratio = w / h

    if current_ratio > TIKTOK_RATIO:
        new_w = int(h * TIKTOK_RATIO)
        left = (w - new_w) // 2
        image = image.crop((left, 0, left + new_w, h))
    elif current_ratio < TIKTOK_RATIO:
        new_h = int(w / TIKTOK_RATIO)
        top = (h - new_h) // 2
        image = image.crop((0, top, w, top + new_h))

    return image


def resize_for_tiktok(image: Image.Image, width: int = 1080) -> Image.Image:
    img = crop_to_tiktok(image)
    height = int(width * 16 / 9)
    return img.resize((width, height), Image.LANCZOS)


def _add_bubble_overlay(image: Image.Image, text: str, position: str) -> Image.Image:
    """Une bulle arrondie par ligne, couleur adaptée à la luminosité du fond."""
    img = crop_to_tiktok(image).copy().convert("RGBA")
    w, h = img.size

    font_size = max(36, int(w * 0.072))
    font = _load_font(font_size)

    pad_x = int(font_size * 0.55)
    pad_y = int(font_size * 0.30)

    lines = textwrap.wrap(text, width=MAX_CHARS_PER_LINE, break_long_words=False, break_on_hyphens=False) or [text]
    draw_tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    line_box_h = font_size + pad_y * 2
    line_step = int(line_box_h * 0.92)
    total_h = line_box_h + line_step * (len(lines) - 1)

    if position == "top":
        y_start = int(h * 0.04)
    elif position == "center":
        y_start = (h - total_h) // 2
    elif position == "belly":
        y_start = int(h * 0.62)
    else:  # bottom
        y_start = int(h * 0.74)

    radius = int(line_box_h * 0.32)
    box_alpha = 140

    bg_region = img.convert("L").crop((0, y_start, w, min(h, y_start + total_h)))
    bg_pixels = list(bg_region.getdata())
    avg_brightness = sum(bg_pixels) / len(bg_pixels) if bg_pixels else 128
    dark_mode = avg_brightness > 160
    bubble_rgb = (30, 30, 30) if dark_mode else (255, 255, 255)

    line_data = []
    for i, line in enumerate(lines):
        bbox = draw_tmp.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        box_w = text_w + pad_x * 2
        box_x = (w - box_w) // 2
        box_y = y_start + i * line_step
        line_data.append({"line": line, "bbox": bbox, "box_x": box_x, "box_y": box_y, "box_w": box_w})

    SCALE = 4
    mask = Image.new("L", (w * SCALE, h * SCALE), 0)
    mask_draw = ImageDraw.Draw(mask)
    for d in line_data:
        bx, by, bw = d["box_x"] * SCALE, d["box_y"] * SCALE, d["box_w"] * SCALE
        bh = line_box_h * SCALE
        mask_draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=radius * SCALE, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)

    boxes_layer = Image.new("RGBA", img.size, (*bubble_rgb, 0))
    scaled_mask = mask.point(lambda v: int(v * box_alpha / 255))
    boxes_layer.putalpha(scaled_mask)

    text_draw = ImageDraw.Draw(boxes_layer)
    for d in line_data:
        line_text_w = d["bbox"][2] - d["bbox"][0]
        text_x = d["box_x"] + (d["box_w"] - line_text_w) // 2 - d["bbox"][0]
        text_y = d["box_y"] + pad_y - d["bbox"][1]
        text_draw.text((text_x, text_y), d["line"], font=font, fill=(255, 255, 255, 255))

    result = Image.alpha_composite(img, boxes_layer)
    return result.convert("RGB")


def _add_outline_overlay(image: Image.Image, text: str, position: str) -> Image.Image:
    """Texte blanc gras avec contour noir épais, sans bulle — style "story"."""
    img = crop_to_tiktok(image).copy().convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    font_size = max(34, int(w * 0.068))
    font = _load_font(font_size)
    stroke = max(3, int(font_size * 0.11))

    lines = textwrap.wrap(text, width=24, break_long_words=False, break_on_hyphens=False) or [text]
    line_h = int(font_size * 1.2)
    total_h = line_h * len(lines)

    if position == "top":
        y_start = int(h * 0.08)
    elif position == "center":
        y_start = (h - total_h) // 2
    elif position == "belly":
        y_start = int(h * 0.60)
    else:  # bottom
        y_start = int(h * 0.78)

    steps = 16
    offsets = [
        (round(stroke * math.cos(2 * math.pi * k / steps)), round(stroke * math.sin(2 * math.pi * k / steps)))
        for k in range(steps)
    ]

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (w - line_w) // 2 - bbox[0]
        y = y_start + i * line_h
        for dx, dy in offsets:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))

    return img


def overlay_text_on_image(image_bytes: bytes, text: str, style: str = "outline", position: str = "top") -> bytes:
    """
    Recadre l'image au format TikTok (9:16) et incruste `text`, selon `style`
    ("bubble" ou "outline") et `position` ("top" | "center" | "belly" | "bottom").
    Renvoie des bytes JPEG.
    """
    text = _strip_emojis(text)
    img = Image.open(BytesIO(image_bytes))

    if not text:
        img = resize_for_tiktok(img)
    elif style == "bubble":
        img = _add_bubble_overlay(img, text, position)
        img = resize_for_tiktok(img)
    else:
        img = _add_outline_overlay(img, text, position)
        img = resize_for_tiktok(img)

    out = BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()
