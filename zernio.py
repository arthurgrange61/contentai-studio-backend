"""
Client Zernio — API d'agrégation multi-plateformes (TikTok, Instagram, Facebook, etc.).

Zernio héberge lui-même les intégrations OAuth déjà validées par chaque
plateforme : on n'a pas besoin de notre propre App Review par réseau social.
Une seule clé API Zernio (côté serveur, jamais exposée) authentifie tout
ContentAI Studio. Chaque client de notre SaaS correspond à un "profile"
Zernio, dans lequel on connecte ses comptes sociaux.

Docs : https://docs.zernio.com
"""
import os

import httpx

BASE_URL = "https://zernio.com/api/v1"


def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _headers() -> dict:
    return {"Authorization": f"Bearer {_cfg('ZERNIO_API_KEY')}"}


async def create_profile(name: str, description: str = "") -> dict:
    """Crée un profile Zernio pour un nouveau client ContentAI Studio."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{BASE_URL}/profiles",
            json={"name": name, "description": description},
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()["profile"]


async def get_connect_url(platform: str, profile_id: str, redirect_url: str) -> str:
    """
    Renvoie l'URL d'autorisation OAuth (hébergée par Zernio) vers laquelle
    rediriger le client pour connecter son compte `platform`.
    """
    params = {"profileId": profile_id, "redirect_url": redirect_url}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BASE_URL}/connect/{platform}", params=params, headers=_headers()
        )
        r.raise_for_status()
        return r.json()["authUrl"]


async def list_accounts(profile_id: str) -> list[dict]:
    """Liste les comptes sociaux connectés pour un profile donné."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BASE_URL}/accounts", params={"profileId": profile_id}, headers=_headers()
        )
        r.raise_for_status()
        return r.json().get("accounts", [])


async def upload_media(filename: str, content_type: str, data: bytes) -> str:
    """
    Téléverse un fichier chez Zernio (flux presign) et renvoie son URL publique,
    utilisable ensuite comme média d'un post. Évite d'avoir à héberger nous-mêmes
    les photos des clients sur un domaine public.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{BASE_URL}/media/presign",
            json={"filename": filename, "contentType": content_type, "size": len(data)},
            headers=_headers(),
        )
        r.raise_for_status()
        info = r.json()
        put = await client.put(
            info["uploadUrl"], content=data, headers={"Content-Type": content_type}
        )
        put.raise_for_status()
        return info["publicUrl"]


def _recycling_config(recurrence: str) -> dict | None:
    """Traduit un choix d'UI ('week' / '2week' / 'month') en config de recyclage Zernio."""
    mapping = {
        "week": {"gap": 1, "gapFreq": "week"},
        "2week": {"gap": 2, "gapFreq": "week"},
        "month": {"gap": 1, "gapFreq": "month"},
    }
    cfg = mapping.get(recurrence)
    if not cfg:
        return None
    return {"enabled": True, **cfg}


async def create_post(
    profile_id: str,
    accounts: list[dict],
    content: str,
    media_urls: list[str],
    media_type: str = "image",
    scheduled_for: str | None = None,
    timezone: str = "Europe/Paris",
    auto_add_music: bool = False,
    recurrence: str = "none",
) -> dict:
    """
    Crée un post multi-plateformes sur `accounts` (objets renvoyés par
    list_accounts). Publie immédiatement si `scheduled_for` est vide, sinon
    programme à cette date. `recurrence` active le recyclage automatique
    (hebdo / bi-hebdo / mensuel — ignoré par TikTok côté Zernio).
    """
    platforms = [
        {"platform": acc["platform"], "accountId": acc["_id"]} for acc in accounts
    ]
    body = {
        "content": content,
        "mediaItems": [{"type": media_type, "url": url} for url in media_urls],
        "platforms": platforms,
        # Réglages TikTok requis pour une publication directe de carrousel photo.
        "tiktokSettings": {
            "privacyLevel": "PUBLIC_TO_EVERYONE",
            "allowComment": True,
            "photoCoverIndex": 0,
            "autoAddMusic": auto_add_music,
            "contentPreviewConfirmed": True,
            "expressConsentGiven": True,
        },
    }

    if scheduled_for:
        body["scheduledFor"] = scheduled_for
        body["timezone"] = timezone
    else:
        body["publishNow"] = True

    recycling = _recycling_config(recurrence)
    if recycling:
        body["recycling"] = recycling

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{BASE_URL}/posts", json=body, headers=_headers())
        try:
            data = r.json()
        except Exception:
            return {"error": {"message": r.text, "http_status": r.status_code}}
        if r.status_code not in (200, 201):
            return {"error": data}
        return data
