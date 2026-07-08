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


async def create_post(
    profile_id: str,
    accounts: list[dict],
    content: str,
    media_urls: list[str],
    media_type: str = "image",
) -> dict:
    """
    Publie immédiatement `content` + les médias sur `accounts` (liste d'objets
    renvoyés par list_accounts — plusieurs plateformes possibles en un appel).
    """
    platforms = [
        {"platform": acc["platform"], "accountId": acc["_id"]} for acc in accounts
    ]
    body = {
        "content": content,
        "mediaItems": [{"type": media_type, "url": url} for url in media_urls],
        "platforms": platforms,
        "publishNow": True,
        "queuedFromProfile": profile_id,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{BASE_URL}/posts", json=body, headers=_headers())
        try:
            data = r.json()
        except Exception:
            return {"error": {"message": r.text, "http_status": r.status_code}}
        if r.status_code not in (200, 201):
            return {"error": data}
        return data
