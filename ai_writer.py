"""
Génération de texte par IA (histoire incrustée sur les photos + légende) via Groq.

Groq expose une API compatible OpenAI (chat completions) — pas besoin de SDK
dédié, un simple appel HTTP suffit. Le client fournit des exemples d'histoires
(une ligne de texte par photo du carrousel, qui se poursuit d'une slide à
l'autre) qui servent de guide de style au modèle.
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


def _format_example_stories(example_stories: list[list[str]]) -> str:
    if not example_stories:
        return "(aucun exemple fourni — improvise un ton vendeur et engageant, adapté à un e-commerce)"
    blocks = []
    for story in example_stories:
        lines = "\n".join(f"  Slide {i + 1} : {line}" for i, line in enumerate(story))
        blocks.append(lines)
    return "\n\n".join(blocks)


async def generate_content_piece(
    business_name: str,
    example_texts: list,
    piece_index: int,
    total_pieces: int,
    num_slides: int = 1,
    max_caption_length: int = 220,
) -> dict:
    """
    Génère une HISTOIRE qui se poursuit slide après slide (une ligne de texte
    par photo du carrousel, `num_slides` lignes au total) + une légende de
    publication, en variant l'angle d'une pièce à l'autre du lot pour éviter
    toute répétition. `example_texts` est une liste d'histoires d'exemple
    (chacune une liste de lignes, dans l'ordre des slides) fournies par le
    client pour guider le ton — accepte aussi l'ancien format (liste de
    chaînes) par rétrocompatibilité.
    `max_caption_length` doit être fixé à 90 si un compte TikTok est ciblé
    (TikTok utilise la légende comme titre du carrousel photo, plafonné à 90
    caractères).
    """
    # Rétrocompatibilité : anciens styles enregistrés avec une simple liste de
    # phrases (pas encore d'histoires multi-slides).
    example_stories = [
        ex if isinstance(ex, list) else [ex]
        for ex in (example_texts or [])
    ]

    examples_block = _format_example_stories(example_stories)

    prompt = f"""Tu écris le texte d'un carrousel storytelling pour la marque "{business_name}", un vendeur e-commerce qui publie sur les réseaux sociaux.

Un carrousel storytelling raconte une petite histoire qui se poursuit de photo en photo : la 1ère slide est une accroche, les suivantes développent l'histoire, et la dernière se termine souvent par un appel à l'action (ex: "lien en bio").

Exemples d'histoires fournies par le client pour te guider sur le TON à adopter (chaque bloc est une histoire complète, une ligne par slide) :
{examples_block}

Tu dois écrire une histoire de EXACTEMENT {num_slides} slide(s) (une ligne de texte par slide, dans l'ordre). C'est la pièce numéro {piece_index + 1} sur un lot de {total_pieces} : IMPORTANT, varie l'angle, l'accroche et la formulation par rapport aux autres pièces du lot pour que le contenu ne soit jamais répétitif.

Chaque ligne doit être courte (mois de 45 caractères si possible) pour tenir sur une photo.

Réponds STRICTEMENT en JSON, sans aucun texte autour, avec ce format exact :
{{"story_lines": [{", ".join(['"ligne slide ' + str(i+1) + '"' for i in range(num_slides)])}], "caption": "légende courte pour la publication, hashtags pertinents inclus — {max_caption_length} caractères MAXIMUM au total, hashtags compris"}}"""

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

        story_lines = parsed.get("story_lines") or []
        # Garantit exactement `num_slides` lignes, quoi que renvoie le modèle.
        story_lines = [str(line)[:90] for line in story_lines][:num_slides]
        while len(story_lines) < num_slides:
            story_lines.append(story_lines[-1] if story_lines else "")

        return {
            "story_lines": story_lines,
            "overlay_text": story_lines[0] if story_lines else "",  # rétrocompatibilité
            # Filet de sécurité : on ne compte pas uniquement sur le respect de la
            # consigne par le modèle, TikTok rejette toute légende > 90 caractères.
            "caption": _truncate_caption(parsed.get("caption", ""), max_caption_length),
        }
