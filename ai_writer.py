"""
Génération de texte par IA (légende + texte à incruster sur les photos) via Groq.

Groq expose une API compatible OpenAI (chat completions) — pas besoin de SDK
dédié, un simple appel HTTP suffit. L'utilisateur fournit des exemples de
textes qui servent de guide de style au modèle.
"""
import json
import os

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _truncate_caption(text: str, limit: int) -> str:
    """Coupe proprement sur un mot entier pour ne jamais dépasser `limit` caractères."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:!-")
    return cut or text[:limit]


async def generate_content_piece(
    business_name: str,
    example_texts: list[str],
    piece_index: int,
    total_pieces: int,
    max_caption_length: int = 220,
) -> dict:
    """
    Génère un texte à incruster sur la photo + une légende de publication,
    en variant le ton d'une pièce à l'autre pour éviter la répétition sur un
    lot de plusieurs contenus. `max_caption_length` doit être fixé à 90 si un
    compte TikTok est ciblé (TikTok utilise la légende comme titre du
    carrousel photo, plafonné à 90 caractères).
    """
    examples_block = (
        "\n".join(f"- {t}" for t in example_texts) if example_texts
        else "(aucun exemple fourni — improvise un ton vendeur et engageant, adapté à un e-commerce)"
    )

    prompt = f"""Tu écris du contenu marketing court pour la marque "{business_name}", un vendeur e-commerce qui publie des carrousels photo sur les réseaux sociaux.

Exemples de textes fournis par le client pour te guider sur le TON à adopter :
{examples_block}

Tu dois générer la pièce de contenu numéro {piece_index + 1} sur un lot de {total_pieces}. Les {total_pieces} pièces seront publiées les unes après les autres : IMPORTANT, varie l'angle et la formulation par rapport aux autres pièces du lot pour que le contenu ne soit jamais répétitif (change l'accroche, l'angle marketing, les emojis, la structure de phrase).

Réponds STRICTEMENT en JSON, sans aucun texte autour, avec ce format exact :
{{"overlay_text": "texte court (5-8 mots max) à incruster directement sur la photo, percutant", "caption": "légende pour la publication, hashtags pertinents inclus — {max_caption_length} caractères MAXIMUM au total, hashtags compris"}}"""

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {_cfg('GROQ_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
            },
        )
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return {
            "overlay_text": parsed.get("overlay_text", "")[:80],
            # Filet de sécurité : on ne compte pas uniquement sur le respect de la
            # consigne par le modèle, TikTok rejette toute légende > 90 caractères.
            "caption": _truncate_caption(parsed.get("caption", ""), max_caption_length),
        }
