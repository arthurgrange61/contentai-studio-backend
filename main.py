"""
ContentAI Studio — SaaS multi-plateformes basé sur l'API Zernio (FastAPI).

Contrairement au projet "ContentAI" historique (qui parle directement aux
API TikTok/Meta avec nos propres apps et notre propre App Review), Studio
route tout via Zernio : chaque client Studio est un "profile" Zernio, et la
connexion de ses comptes sociaux passe par le flux OAuth hébergé par Zernio
(déjà validé côté plateformes).

Routes :
  GET  /                     → formulaire d'inscription (nom + email uniquement)
  POST /signup               → crée le profile Zernio + session
  GET  /dashboard            → aperçu produit + formulaire de post (accessible sans compte connecté)
  GET  /settings             → comptes connectés + boutons de connexion
  GET  /connect/{platform}   → redirige vers l'autorisation Zernio
  GET  /connect/callback     → retour Zernio après connexion d'un compte
  POST /api/post             → publie sur les comptes sélectionnés
  GET  /logout
"""
import os
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature

import zernio

# Chemin absolu : le .env est chargé quel que soit le dossier de lancement
# (sur Render les variables sont injectées directement, load_dotenv est alors un no-op).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

app = FastAPI(title="ContentAI Studio — Zernio integration")
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
signer = URLSafeSerializer(SESSION_SECRET, salt="contentai-studio-session")

# studio_user_id -> { profile_id, business_name, email }
SESSIONS: dict[str, dict] = {}


def _set_session(resp, studio_user_id: str):
    resp.set_cookie(
        "contentai_studio_sid",
        signer.dumps({"studio_user_id": studio_user_id}),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )


def _get_session(request: Request) -> dict | None:
    raw = request.cookies.get("contentai_studio_sid")
    if not raw:
        return None
    try:
        data = signer.loads(raw)
    except BadSignature:
        return None
    return SESSIONS.get(data.get("studio_user_id"))


def _base_url(request: Request) -> str:
    # Derrière Render (proxy Cloudflare), request.url peut renvoyer http:// en interne.
    return str(request.base_url).rstrip("/").replace("http://", "https://")


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    sess = _get_session(request)
    if sess:
        return RedirectResponse("/dashboard")
    configured = bool(os.environ.get("ZERNIO_API_KEY"))
    return templates.TemplateResponse("index.html", {"request": request, "configured": configured})


@app.post("/signup", response_class=HTMLResponse)
async def signup(request: Request, business_name: str = Form(...), email: str = Form(...)):
    profile = await zernio.create_profile(name=business_name, description=f"ContentAI Studio — {email}")

    studio_user_id = secrets.token_hex(12)
    SESSIONS[studio_user_id] = {
        "profile_id": profile["_id"],
        "business_name": business_name,
        "email": email,
    }
    resp = RedirectResponse("/dashboard", status_code=303)
    _set_session(resp, studio_user_id)
    return resp


@app.api_route("/dashboard", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard(request: Request):
    sess = _get_session(request)
    if not sess:
        return RedirectResponse("/")

    accounts = await zernio.list_accounts(sess["profile_id"])
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "business_name": sess["business_name"], "accounts": accounts},
    )


@app.api_route("/settings", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def settings(request: Request):
    sess = _get_session(request)
    if not sess:
        return RedirectResponse("/")

    accounts = await zernio.list_accounts(sess["profile_id"])
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "business_name": sess["business_name"], "email": sess["email"], "accounts": accounts},
    )


@app.api_route("/connect/callback", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def connect_callback(request: Request):
    # Zernio (mode standard) gère lui-même l'échange OAuth et nous redirige ici
    # avec ?connected={platform}&profileId=X&accountId=Y&username=Z — rien à faire
    # de notre côté, le compte est déjà connecté côté Zernio.
    # Doit être déclarée AVANT /connect/{platform}, sinon FastAPI route "callback"
    # comme si c'était une plateforme.
    return RedirectResponse("/settings")


@app.get("/connect/{platform}")
async def connect(request: Request, platform: str):
    sess = _get_session(request)
    if not sess:
        return RedirectResponse("/")

    redirect_url = f"{_base_url(request)}/connect/callback"
    auth_url = await zernio.get_connect_url(
        platform=platform, profile_id=sess["profile_id"], redirect_url=redirect_url
    )
    return RedirectResponse(auth_url)


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}


@app.post("/api/post", response_class=HTMLResponse)
async def api_post(
    request: Request,
    account_ids: list[str] = Form(default=[]),
    caption: str = Form(...),
    photos: list[UploadFile] = File(default=[]),
    schedule_mode: str = Form("now"),          # "now" | "scheduled"
    scheduled_for: str = Form(""),
    timezone: str = Form("Europe/Paris"),
    recurrence: str = Form("none"),            # "none" | "week" | "2week" | "month"
    auto_add_music: str = Form("off"),
):
    sess = _get_session(request)
    if not sess:
        return RedirectResponse("/", status_code=303)

    accounts = await zernio.list_accounts(sess["profile_id"])
    selected = [a for a in accounts if a["_id"] in account_ids]

    def _render(result):
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "business_name": sess["business_name"], "accounts": accounts, "result": result},
        )

    if not selected:
        return _render({"error": {"message": "Sélectionne au moins un compte cible."}})

    # Téléverse chaque photo chez Zernio et récupère son URL publique.
    real_photos = [p for p in photos if p and p.filename]
    if not real_photos:
        return _render({"error": {"message": "Ajoute au moins une photo."}})
    if len(real_photos) > 10:
        return _render({"error": {"message": "10 photos maximum par carrousel."}})

    media_urls = []
    for photo in real_photos:
        content_type = (photo.content_type or "image/jpeg").lower()
        if content_type not in ALLOWED_IMAGE_TYPES:
            return _render({"error": {"message": f"Format non supporté : {content_type}. Utilise JPG, PNG, WEBP ou GIF."}})
        data = await photo.read()
        try:
            url = await zernio.upload_media(photo.filename, content_type, data)
        except Exception as e:
            return _render({"error": {"message": f"Échec de l'envoi d'une photo : {e}"}})
        media_urls.append(url)

    result = await zernio.create_post(
        profile_id=sess["profile_id"],
        accounts=selected,
        content=caption,
        media_urls=media_urls,
        scheduled_for=(scheduled_for if schedule_mode == "scheduled" else None),
        timezone=timezone,
        auto_add_music=(auto_add_music == "on"),
        recurrence=recurrence,
    )

    return _render(result)


@app.api_route("/cgu", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def cgu(request: Request):
    return templates.TemplateResponse("cgu.html", {"request": request})


@app.api_route("/confidentialite", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def confidentialite(request: Request):
    return templates.TemplateResponse("confidentialite.html", {"request": request})


@app.api_route("/logout", methods=["GET", "HEAD"])
async def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie("contentai_studio_sid")
    return resp


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    return {"status": "ok"}
