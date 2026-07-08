"""
ContentAI Studio — SaaS multi-plateformes basé sur l'API Zernio (FastAPI).

Contrairement au projet "ContentAI" historique (qui parle directement aux
API TikTok/Meta avec nos propres apps et notre propre App Review), Studio
route tout via Zernio : chaque client Studio est un "profile" Zernio, et la
connexion de ses comptes sociaux passe par le flux OAuth hébergé par Zernio
(déjà validé côté plateformes).

Routes principales :
  GET  /                     → formulaire d'inscription (nom + email uniquement)
  POST /signup               → crée/retrouve le compte Studio en DB — AUCUN appel Zernio
  GET  /dashboard            → aperçu produit + formulaire de post ponctuel
  GET  /settings             → comptes connectés + boutons de connexion
  GET  /connect/{platform}   → crée le profile Zernio si besoin, puis redirige vers l'autorisation
  GET  /connect/callback     → retour Zernio après connexion d'un compte
  POST /api/post             → publie un carrousel ponctuel

  GET  /library              → bibliothèque de photos (upload en masse)
  POST /library/upload
  POST /library/delete/{photo_id}

  GET  /styles               → styles de contenu (exemples de textes pour guider l'IA)
  POST /styles
  POST /styles/delete/{style_id}

  GET  /generate             → génération par lot (auto ou semi-manuel)
  POST /generate             → lance la génération en tâche de fond
  GET  /queue                → suivi de la file de contenus générés/programmés

  GET  /logout

⚠️ Le profile Zernio n'est créé qu'à la toute première connexion d'un compte
   social (voir _ensure_profile) — l'inscription elle-même (email + nom de
   boutique) est gérée entièrement par nous, sans dépendre du quota Zernio.
"""
import datetime
import os
import random
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature
from sqlalchemy import select

import ai_writer
import db
import imaging
import zernio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="ContentAI Studio — Zernio integration", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
signer = URLSafeSerializer(SESSION_SECRET, salt="contentai-studio-session")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
RECURRENCE_DAYS = {"none": None, "daily": 1, "2days": 2, "3days": 3, "weekly": 7}


# ─── Session (cookie signé -> user_id, l'utilisateur lui-même vit en DB) ────
def _set_session(resp, user_id: str):
    resp.set_cookie(
        "contentai_studio_sid",
        signer.dumps({"user_id": user_id}),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )


def _session_user_id(request: Request) -> str | None:
    raw = request.cookies.get("contentai_studio_sid")
    if not raw:
        return None
    try:
        data = signer.loads(raw)
    except BadSignature:
        return None
    return data.get("user_id")


def _base_url(request: Request) -> str:
    # Derrière Render (proxy Cloudflare), request.url peut renvoyer http:// en interne.
    return str(request.base_url).rstrip("/").replace("http://", "https://")


async def _ensure_profile(session, user: db.StudioUser) -> str:
    """Crée le profile Zernio à la demande (1ère connexion d'un compte social)."""
    if not user.profile_id:
        profile = await zernio.create_profile(
            name=user.business_name, description=f"ContentAI Studio — {user.email}"
        )
        user.profile_id = profile["_id"]
        await session.commit()
    return user.profile_id


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    if _session_user_id(request):
        return RedirectResponse("/dashboard")
    configured = bool(os.environ.get("ZERNIO_API_KEY"))
    return templates.TemplateResponse("index.html", {"request": request, "configured": configured})


@app.post("/signup", response_class=HTMLResponse)
async def signup(request: Request, business_name: str = Form(...), email: str = Form(...)):
    # Aucun appel à Zernio ici : le compte Studio (email + boutique) est géré
    # entièrement en DB. Le profile Zernio n'est créé qu'à la 1ère connexion
    # d'un réseau social (voir /connect/{platform}).
    async with db.get_session() as session:
        user = await db.get_user_by_email(session, email)
        if not user:
            user = db.StudioUser(business_name=business_name, email=email)
            session.add(user)
            await session.commit()
            await session.refresh(user)

    resp = RedirectResponse("/dashboard", status_code=303)
    _set_session(resp, user.id)
    return resp


@app.api_route("/dashboard", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        accounts = await zernio.list_accounts(user.profile_id) if user.profile_id else []
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "business_name": user.business_name, "accounts": accounts},
        )


@app.api_route("/settings", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def settings(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        accounts = await zernio.list_accounts(user.profile_id) if user.profile_id else []
        return templates.TemplateResponse(
            "settings.html",
            {"request": request, "business_name": user.business_name, "email": user.email, "accounts": accounts},
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
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        profile_id = await _ensure_profile(session, user)

    redirect_url = f"{_base_url(request)}/connect/callback"
    auth_url = await zernio.get_connect_url(platform=platform, profile_id=profile_id, redirect_url=redirect_url)
    return RedirectResponse(auth_url)


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
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/", status_code=303)

        accounts = await zernio.list_accounts(user.profile_id) if user.profile_id else []
        selected = [a for a in accounts if a["_id"] in account_ids]

        def _render(result):
            return templates.TemplateResponse(
                "dashboard.html",
                {"request": request, "business_name": user.business_name, "accounts": accounts, "result": result},
            )

        if not selected:
            return _render({"error": {"message": "Sélectionne au moins un compte cible."}})

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
            profile_id=user.profile_id,
            accounts=selected,
            content=caption,
            media_urls=media_urls,
            scheduled_for=(scheduled_for if schedule_mode == "scheduled" else None),
            timezone=timezone,
            auto_add_music=(auto_add_music == "on"),
            recurrence=recurrence,
        )
        return _render(result)


# ─── Bibliothèque de photos ─────────────────────────────────────────────────
@app.api_route("/library", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def library(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        photos = await db.list_photos(session, user_id)
        return templates.TemplateResponse(
            "library.html",
            {"request": request, "business_name": user.business_name, "photos": photos},
        )


@app.post("/library/upload", response_class=HTMLResponse)
async def library_upload(request: Request, photos: list[UploadFile] = File(default=[])):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    real_photos = [p for p in photos if p and p.filename]

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/", status_code=303)

        errors = []
        for photo in real_photos:
            content_type = (photo.content_type or "image/jpeg").lower()
            if content_type not in ALLOWED_IMAGE_TYPES:
                errors.append(f"{photo.filename} : format non supporté")
                continue
            data = await photo.read()
            try:
                url = await zernio.upload_media(photo.filename, content_type, data)
            except Exception as e:
                errors.append(f"{photo.filename} : échec de l'envoi ({e})")
                continue
            session.add(db.Photo(user_id=user_id, url=url, filename=photo.filename))
        await session.commit()

        photos_list = await db.list_photos(session, user_id)
        return templates.TemplateResponse(
            "library.html",
            {"request": request, "business_name": user.business_name, "photos": photos_list, "errors": errors},
        )


@app.post("/library/delete/{photo_id}")
async def library_delete(photo_id: str, request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        photo = await session.get(db.Photo, photo_id)
        if photo and photo.user_id == user_id:
            await session.delete(photo)
            await session.commit()
    return RedirectResponse("/library", status_code=303)


# ─── Styles de contenu ───────────────────────────────────────────────────────
@app.api_route("/styles", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def styles_page(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        styles = await db.list_styles(session, user_id)
        return templates.TemplateResponse(
            "styles.html",
            {"request": request, "business_name": user.business_name, "styles": styles},
        )


@app.post("/styles", response_class=HTMLResponse)
async def styles_create(request: Request, name: str = Form(...), examples: str = Form("")):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    example_texts = [line.strip() for line in examples.splitlines() if line.strip()]

    async with db.get_session() as session:
        session.add(db.ContentStyle(user_id=user_id, name=name, example_texts=example_texts))
        await session.commit()

    return RedirectResponse("/styles", status_code=303)


@app.post("/styles/delete/{style_id}")
async def styles_delete(style_id: str, request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        style = await session.get(db.ContentStyle, style_id)
        if style and style.user_id == user_id:
            await session.delete(style)
            await session.commit()
    return RedirectResponse("/styles", status_code=303)


# ─── Génération par lot ──────────────────────────────────────────────────────
@app.api_route("/generate", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def generate_page(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        photos = await db.list_photos(session, user_id)
        styles = await db.list_styles(session, user_id)
        accounts = await zernio.list_accounts(user.profile_id) if user.profile_id else []
        return templates.TemplateResponse(
            "generate.html",
            {
                "request": request,
                "business_name": user.business_name,
                "photos": photos,
                "styles": styles,
                "accounts": accounts,
            },
        )


async def _run_batch(user_id: str, content_ids: list[str], account_ids: list[str]):
    """Tâche de fond : génère le texte IA, incruste sur la photo, publie/programme via Zernio."""
    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user or not user.profile_id:
            return
        accounts = await zernio.list_accounts(user.profile_id)
        selected_accounts = [a for a in accounts if a["_id"] in account_ids]
        if not selected_accounts:
            return

        total = len(content_ids)
        for idx, content_id in enumerate(content_ids):
            item = await session.get(db.GeneratedContent, content_id)
            if not item:
                continue

            style_examples = []
            if item.style_id:
                style = await session.get(db.ContentStyle, item.style_id)
                if style:
                    style_examples = style.example_texts or []

            try:
                ai_result = await ai_writer.generate_content_piece(
                    business_name=user.business_name,
                    example_texts=style_examples,
                    piece_index=idx,
                    total_pieces=total,
                )
                photo_url = item.photo_urls[0]
                async with httpx.AsyncClient(timeout=30) as client:
                    photo_bytes = (await client.get(photo_url)).content
                composed = imaging.overlay_text_on_image(photo_bytes, ai_result["overlay_text"])
                composed_url = await zernio.upload_media(f"generated_{content_id}.jpg", "image/jpeg", composed)

                scheduled_iso = item.scheduled_for.isoformat() if item.scheduled_for else None
                result = await zernio.create_post(
                    profile_id=user.profile_id,
                    accounts=selected_accounts,
                    content=ai_result["caption"],
                    media_urls=[composed_url],
                    scheduled_for=scheduled_iso,
                    timezone="Europe/Paris",
                )

                item.overlay_text = ai_result["overlay_text"]
                item.caption = ai_result["caption"]
                item.composed_urls = [composed_url]
                if result.get("error"):
                    item.status = "failed"
                    item.error = str(result["error"])
                else:
                    item.status = "scheduled"
                    post = result.get("post", {})
                    item.zernio_post_id = post.get("_id")
            except Exception as e:
                item.status = "failed"
                item.error = str(e)

            await session.commit()


@app.post("/generate", response_class=HTMLResponse)
async def generate_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    mode: str = Form("auto"),                  # "auto" | "manual"
    count: int = Form(10),
    photo_order: str = Form(""),               # manuel : ids séparés par des virgules, dans l'ordre
    style_id: str = Form(""),
    start_date: str = Form(...),
    start_time: str = Form("09:00"),
    recurrence: str = Form("daily"),           # "daily" | "2days" | "3days" | "weekly"
    account_ids: list[str] = Form(default=[]),
):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user or not user.profile_id or not account_ids:
            return RedirectResponse("/generate", status_code=303)

        photos = await db.list_photos(session, user_id)
        if not photos:
            return RedirectResponse("/generate", status_code=303)
        by_id = {p.id: p for p in photos}

        if mode == "manual" and photo_order.strip():
            ordered_ids = [pid for pid in photo_order.split(",") if pid in by_id]
        else:
            pool = photos.copy()
            random.shuffle(pool)
            ordered_ids = []
            i = 0
            while len(ordered_ids) < max(1, count):
                ordered_ids.append(pool[i % len(pool)].id)
                i += 1

        try:
            start_dt = datetime.datetime.fromisoformat(f"{start_date}T{start_time}:00")
        except ValueError:
            start_dt = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        step_days = RECURRENCE_DAYS.get(recurrence, 1) or 1

        content_ids = []
        for idx, photo_id in enumerate(ordered_ids):
            scheduled_for = start_dt + datetime.timedelta(days=step_days * idx)
            item = db.GeneratedContent(
                user_id=user_id,
                style_id=(style_id or None),
                photo_urls=[by_id[photo_id].url],
                scheduled_for=scheduled_for,
                status="pending",
            )
            session.add(item)
            await session.flush()
            content_ids.append(item.id)
        await session.commit()

    background_tasks.add_task(_run_batch, user_id, content_ids, account_ids)
    return RedirectResponse("/queue", status_code=303)


@app.api_route("/queue", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def queue_page(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        contents = await db.list_contents(session, user_id)
        pending = any(c.status == "pending" for c in contents)
        return templates.TemplateResponse(
            "queue.html",
            {"request": request, "business_name": user.business_name, "contents": contents, "pending": pending},
        )


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
