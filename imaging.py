"""
Incrustation de texte sur une photo (Pillow) — bandeau semi-transparent en
bas de l'image + texte blanc, avec retour à la ligne automatique.
"""
import io
import os
import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "Poppins-Bold.ttf")

# Poppins (comme la plupart des polices TTF classiques) n'a pas de glyphes emoji couleur ;
# on les retire du texte incrusté sur l'image (la légende du post les garde, elle).
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def overlay_text_on_image(image_bytes: bytes, text: str) -> bytes:
    """Renvoie les bytes JPEG de l'image avec `text` incrusté en bas, sur un bandeau sombre."""
    text = _strip_emoji(text)
    if not text:
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size

    font_size = max(24, w // 18)
    font = ImageFont.truetype(FONT_PATH, font_size)

    draw = ImageDraw.Draw(img, "RGBA")
    # Largeur de ligne approximative en nombre de caractères pour ce corps de police.
    avg_char_w = font.getlength("x") or (font_size * 0.55)
    max_chars = max(10, int((w * 0.86) / avg_char_w))
    lines = textwrap.wrap(text, width=max_chars) or [text]

    line_height = int(font_size * 1.25)
    band_height = line_height * len(lines) + int(font_size * 1.2)
    band_top = h - band_height

    draw.rectangle([(0, band_top), (w, h)], fill=(0, 0, 0, 150))

    y = band_top + int(font_size * 0.6)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (w - line_w) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()
