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


async def generate_content_piece(
    business_name: str,
    example_texts: list[str],
    piece_index: int,
    total_pieces: int,
) -> dict:
    """
    Génère un texte à incruster sur la photo + une légende de publication,
    en variant le ton d'une pièce à l'autre pour éviter la répétition sur un
    lot de plusieurs contenus.
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
{{"overlay_text": "texte court (5-8 mots max) à incruster directement sur la photo, percutant", "caption": "légende complète pour la publication (2-3 phrases, avec des hashtags pertinents)"}}"""

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
            "caption": parsed.get("caption", ""),
        }
