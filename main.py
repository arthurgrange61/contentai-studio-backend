"""
ContentAI Studio — SaaS multi-plateformes basé sur l'API Zernio (FastAPI).

Contrairement au projet "ContentAI" historique (qui parle directement aux
API TikTok/Meta avec nos propres apps et notre propre App Review), Studio
route tout via Zernio : chaque client Studio est un "profile" Zernio, et la
connexion de ses comptes sociaux passe par le flux OAuth hébergé par Zernio
(déjà validé côté plateformes).

Routes principales — 3 espaces : Bibliothèque, Styles, Publication.
  GET  /                     → formulaire d'inscription (nom + email uniquement)
  POST /signup               → crée/retrouve le compte Studio en DB — AUCUN appel Zernio
  GET  /settings             → comptes connectés + boutons de connexion
  GET  /connect/{platform}   → crée le profile Zernio si besoin, puis redirige vers l'autorisation
  GET  /connect/callback     → retour Zernio après connexion d'un compte

  GET  /library              → bibliothèque de photos (upload en masse)
  POST /library/upload
  POST /library/delete/{photo_id}

  GET  /styles               → recettes de génération (nb de photos, musique, position du
                                texte incrusté, exemples de textes pour guider l'IA)
  POST /styles
  POST /styles/delete/{style_id}

  GET  /posting              → planning récurrent (quel style, quel jour, quelle heure)
  POST /posting/rules        → ajoute une règle récurrente
  POST /posting/rules/delete/{rule_id}
  POST /posting/generate     → génère les prochaines occurrences en tâche de fond
  GET  /queue                → suivi de la file de contenus générés/programmés

  POST /billing/checkout     → crée une session Stripe Checkout pour un plan
  GET  /billing/portal       → ouvre le Customer Portal Stripe (gérer/annuler)
  POST /billing/webhook      → webhook Stripe (statut d'abonnement à jour)

  GET  /logout

⚠️ Le profile Zernio n'est créé qu'à la toute première connexion d'un compte
   social (voir _ensure_profile) — l'inscription elle-même (email + nom de
   boutique) est gérée entièrement par nous, sans dépendre du quota Zernio.
"""
import datetime
import os
import base64
import random
import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Doit être chargé AVANT nos modules locaux : plusieurs d'entre eux (db.py,
# billing.py...) lisent des variables d'environnement dès l'import (création
# du moteur SQLAlchemy, clé API Stripe...). Sur Render ça ne se voyait pas
# (les variables sont injectées directement dans l'environnement du process),
# mais en local, sans .env déjà chargé, ces modules démarraient avec des
# valeurs vides.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature

import ai_writer
import billing
import db
import imaging
import zernio


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


@app.exception_handler(httpx.HTTPStatusError)
async def zernio_error_handler(request: Request, exc: httpx.HTTPStatusError):
    """
    Toute erreur HTTP non gérée venant de Zernio (quota de comptes/profiles
    atteint, panne côté API...) affiche une page claire plutôt qu'un 500 brut.
    """
    status = exc.response.status_code
    if status == 402:
        title = "Erreur du nombre de connexions"
        message = "Erreur du nombre de connexions, contactez le support."
    else:
        title = "Service momentanément indisponible"
        message = "Une erreur technique est survenue. Réessaie dans un instant, ou contacte le support si ça persiste."
    return templates.TemplateResponse(
        "error.html", {"request": request, "title": title, "message": message}, status_code=502
    )


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
        return RedirectResponse("/library")
    configured = bool(os.environ.get("ZERNIO_API_KEY"))
    return templates.TemplateResponse("index.html", {"request": request, "configured": configured})


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/signup", response_class=HTMLResponse)
async def signup(request: Request, business_name: str = Form(...), email: str = Form(...)):
    # Aucun appel à Zernio ici : le compte Studio (email + boutique) est géré
    # entièrement en DB. Le profile Zernio n'est créé qu'à la 1ère connexion
    # d'un réseau social (voir /connect/{platform}).
    if not EMAIL_RE.match(email):
        configured = bool(os.environ.get("ZERNIO_API_KEY"))
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "configured": configured, "signup_error": "Adresse e-mail invalide."},
        )

    async with db.get_session() as session:
        user = await db.get_user_by_email(session, email)
        if not user:
            user = db.StudioUser(business_name=business_name, email=email)
            session.add(user)
            await session.commit()
            await session.refresh(user)

    resp = RedirectResponse("/library", status_code=303)
    _set_session(resp, user.id)
    return resp


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
            {
                "request": request,
                "business_name": user.business_name,
                "email": user.email,
                "accounts": accounts,
                "plan": user.plan,
                "subscription_status": user.subscription_status,
                "plans": billing.PLANS,
            },
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
        if user.subscription_status != "active":
            return RedirectResponse("/settings?subscribe_required=1")

        plan_cfg = billing.PLANS.get(user.plan)
        max_accounts = plan_cfg["max_accounts"] if plan_cfg else 0
        profile_id = await _ensure_profile(session, user)

    current_accounts = await zernio.list_accounts(profile_id)
    if len(current_accounts) >= max_accounts:
        return RedirectResponse("/settings?account_limit=1")

    redirect_url = f"{_base_url(request)}/connect/callback"
    auth_url = await zernio.get_connect_url(platform=platform, profile_id=profile_id, redirect_url=redirect_url)
    return RedirectResponse(auth_url)


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
async def styles_create(
    request: Request,
    name: str = Form(...),
    examples: str = Form(""),
    photo_count: int = Form(1),
    overlay_position: str = Form("first"),   # "none" | "first" | "last" | "all"
    music_enabled: str = Form("off"),
    text_style: str = Form("outline"),       # "bubble" | "outline"
    text_placement: str = Form("top"),       # "top" | "center" | "belly" | "bottom"
):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    example_texts = [line.strip() for line in examples.splitlines() if line.strip()]
    photo_count = max(1, min(10, photo_count))

    async with db.get_session() as session:
        session.add(db.ContentStyle(
            user_id=user_id,
            name=name,
            example_texts=example_texts,
            photo_count=photo_count,
            overlay_position=overlay_position,
            music_enabled=(music_enabled == "on"),
            text_style=text_style,
            text_placement=text_placement,
        ))
        await session.commit()

    return RedirectResponse("/styles", status_code=303)


@app.post("/styles/{style_id}/edit", response_class=HTMLResponse)
async def styles_edit(
    style_id: str,
    request: Request,
    name: str = Form(...),
    examples: str = Form(""),
    photo_count: int = Form(1),
    overlay_position: str = Form("first"),
    music_enabled: str = Form("off"),
    text_style: str = Form("outline"),
    text_placement: str = Form("top"),
):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    example_texts = [line.strip() for line in examples.splitlines() if line.strip()]
    photo_count = max(1, min(10, photo_count))

    async with db.get_session() as session:
        style = await session.get(db.ContentStyle, style_id)
        if style and style.user_id == user_id:
            style.name = name
            style.example_texts = example_texts
            style.photo_count = photo_count
            style.overlay_position = overlay_position
            style.music_enabled = (music_enabled == "on")
            style.text_style = text_style
            style.text_placement = text_placement
            await session.commit()

    return RedirectResponse("/styles", status_code=303)


@app.get("/styles/{style_id}/preview", response_class=HTMLResponse)
async def styles_preview(style_id: str, request: Request):
    """Génère 1 exemple de carrousel complet pour ce style, sans rien publier ni stocker chez Zernio."""
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        style = await session.get(db.ContentStyle, style_id)
        if not user or not style or style.user_id != user_id:
            return RedirectResponse("/styles")

        photos = await db.list_photos(session, user_id)
        if not photos:
            return RedirectResponse("/library")

        pool = photos.copy()
        random.shuffle(pool)
        chosen = [pool[i % len(pool)] for i in range(style.photo_count)]

        ai_result = await ai_writer.generate_content_piece(
            business_name=user.business_name,
            example_texts=style.example_texts or [],
            piece_index=0,
            total_pieces=1,
        )

        n = len(chosen)
        overlay_indexes = {
            "none": set(),
            "first": {0},
            "last": {n - 1},
            "all": set(range(n)),
        }.get(style.overlay_position, {0})

        carousel_images = []
        async with httpx.AsyncClient(timeout=30) as client:
            for i, photo in enumerate(chosen):
                photo_bytes = (await client.get(photo.url)).content
                text = ai_result["overlay_text"] if i in overlay_indexes else ""
                composed = imaging.overlay_text_on_image(
                    photo_bytes, text, style=style.text_style, position=style.text_placement
                )
                carousel_images.append("data:image/jpeg;base64," + base64.b64encode(composed).decode())

        return templates.TemplateResponse(
            "style_preview.html",
            {
                "request": request,
                "business_name": user.business_name,
                "style": style,
                "carousel_images": carousel_images,
                "caption": ai_result["caption"],
            },
        )


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


# ─── Publication automatique (planning récurrent par jour + style) ─────────
WEEKDAY_LABELS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]


def _next_occurrence(day_of_week: int, time_str: str, now: datetime.datetime | None = None) -> datetime.datetime:
    now = now or datetime.datetime.utcnow()
    hour, minute = (int(p) for p in time_str.split(":"))
    days_until = (day_of_week - now.weekday()) % 7
    candidate = (now + datetime.timedelta(days=days_until)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=7)
    return candidate


@app.api_route("/posting", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def posting_page(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/")
        styles = await db.list_styles(session, user_id)
        rules = await db.list_posting_rules(session, user_id)
        accounts = await zernio.list_accounts(user.profile_id) if user.profile_id else []

        rules_by_day = {i: [] for i in range(7)}
        next_occurrence = {}
        for rule in rules:
            rules_by_day[rule.day_of_week].append(rule)
            if rule.active:
                next_occurrence[rule.id] = _next_occurrence(rule.day_of_week, rule.time)

        return templates.TemplateResponse(
            "posting.html",
            {
                "request": request,
                "business_name": user.business_name,
                "styles": styles,
                "rules": rules,
                "rules_by_day": rules_by_day,
                "next_occurrence": next_occurrence,
                "accounts": accounts,
                "weekday_labels": WEEKDAY_LABELS,
            },
        )


@app.post("/posting/rules", response_class=HTMLResponse)
async def posting_rule_create(
    request: Request,
    style_id: str = Form(...),
    day_of_week: int = Form(...),
    time: str = Form("09:00"),
    account_ids: list[str] = Form(default=[]),
):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        session.add(db.PostingRule(
            user_id=user_id, style_id=style_id, day_of_week=day_of_week, time=time, account_ids=account_ids,
        ))
        await session.commit()

    return RedirectResponse("/posting", status_code=303)


@app.post("/posting/rules/{rule_id}/toggle")
async def posting_rule_toggle(rule_id: str, request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        rule = await session.get(db.PostingRule, rule_id)
        if rule and rule.user_id == user_id:
            rule.active = not rule.active
            await session.commit()
    return RedirectResponse("/posting", status_code=303)


@app.post("/posting/rules/delete/{rule_id}")
async def posting_rule_delete(rule_id: str, request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        rule = await session.get(db.PostingRule, rule_id)
        if rule and rule.user_id == user_id:
            await session.delete(rule)
            await session.commit()
    return RedirectResponse("/posting", status_code=303)


@app.post("/posting/publish-now")
async def posting_publish_now(
    request: Request,
    background_tasks: BackgroundTasks,
    style_id: str = Form(...),
    account_ids: list[str] = Form(default=[]),
):
    """Génère et publie immédiatement une pièce de contenu avec ce style, sans passer par le planning."""
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    if not account_ids:
        return RedirectResponse("/posting", status_code=303)

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        style = await session.get(db.ContentStyle, style_id)
        if not user or not user.profile_id or not style:
            return RedirectResponse("/posting", status_code=303)

        photos = await db.list_photos(session, user_id)
        if not photos:
            return RedirectResponse("/library", status_code=303)

        pool = photos.copy()
        random.shuffle(pool)
        chosen = [pool[i % len(pool)] for i in range(style.photo_count)]

        item = db.GeneratedContent(
            user_id=user_id,
            style_id=style.id,
            photo_urls=[p.url for p in chosen],
            account_ids=account_ids,
            scheduled_for=None,  # None -> Zernio publie immédiatement (publishNow)
            status="pending",
        )
        session.add(item)
        await session.commit()
        content_id = item.id

    background_tasks.add_task(_run_batch, user_id, [content_id])
    return RedirectResponse("/queue", status_code=303)


def _draw_photos(pool: list[db.Photo], cursor: list[int], n: int) -> list[db.Photo]:
    """Pioche `n` photos dans `pool` en tournant dessus (évite les répétitions immédiates)."""
    if not pool:
        return []
    picked = []
    for _ in range(n):
        if cursor[0] % len(pool) == 0 and cursor[0] != 0:
            random.shuffle(pool)
        picked.append(pool[cursor[0] % len(pool)])
        cursor[0] += 1
    return picked


async def _run_batch(user_id: str, content_ids: list[str]):
    """Tâche de fond : génère le texte IA, incruste sur les photos, publie/programme via Zernio."""
    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user or not user.profile_id:
            return
        all_accounts = await zernio.list_accounts(user.profile_id)
        accounts_by_id = {a["_id"]: a for a in all_accounts}

        total = len(content_ids)
        for idx, content_id in enumerate(content_ids):
            item = await session.get(db.GeneratedContent, content_id)
            if not item:
                continue

            style = await session.get(db.ContentStyle, item.style_id) if item.style_id else None
            style_examples = (style.example_texts if style else []) or []
            overlay_position = style.overlay_position if style else "first"
            music_enabled = style.music_enabled if style else False
            text_style = style.text_style if style else "outline"
            text_placement = style.text_placement if style else "top"

            selected_accounts = [accounts_by_id[aid] for aid in item.account_ids if aid in accounts_by_id]
            if not selected_accounts:
                item.status = "failed"
                item.error = "Aucun compte cible valide (déconnecté ?)."
                await session.commit()
                continue

            try:
                # TikTok utilise la légende comme titre du carrousel photo, plafonné
                # à 90 caractères — les autres plateformes sont bien plus larges.
                has_tiktok = any(a.get("platform") == "tiktok" for a in selected_accounts)
                ai_result = await ai_writer.generate_content_piece(
                    business_name=user.business_name,
                    example_texts=style_examples,
                    piece_index=idx,
                    total_pieces=total,
                    max_caption_length=90 if has_tiktok else 220,
                )

                n = len(item.photo_urls)
                overlay_indexes = {
                    "none": set(),
                    "first": {0},
                    "last": {n - 1},
                    "all": set(range(n)),
                }.get(overlay_position, {0})

                composed_urls = []
                async with httpx.AsyncClient(timeout=30) as client:
                    for i, photo_url in enumerate(item.photo_urls):
                        if i in overlay_indexes:
                            photo_bytes = (await client.get(photo_url)).content
                            composed = imaging.overlay_text_on_image(
                                photo_bytes, ai_result["overlay_text"], style=text_style, position=text_placement
                            )
                            composed_urls.append(
                                await zernio.upload_media(f"generated_{content_id}_{i}.jpg", "image/jpeg", composed)
                            )
                        else:
                            composed_urls.append(photo_url)

                # +5 min de marge côté Zernio pour éviter les bugs de publication trop
                # proche de "maintenant" (l'heure affichée au client reste inchangée,
                # seul l'appel à Zernio est décalé).
                scheduled_iso = (
                    (item.scheduled_for + datetime.timedelta(minutes=5)).isoformat()
                    if item.scheduled_for else None
                )
                result = await zernio.create_post(
                    profile_id=user.profile_id,
                    accounts=selected_accounts,
                    content=ai_result["caption"],
                    media_urls=composed_urls,
                    scheduled_for=scheduled_iso,
                    timezone="Europe/Paris",
                    auto_add_music=music_enabled,
                )

                item.overlay_text = ai_result["overlay_text"]
                item.caption = ai_result["caption"]
                item.composed_urls = composed_urls
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


@app.post("/posting/generate", response_class=HTMLResponse)
async def posting_generate(
    request: Request,
    background_tasks: BackgroundTasks,
    weeks_ahead: int = Form(2),
):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/", status_code=303)

    weeks_ahead = max(1, min(8, weeks_ahead))

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user or not user.profile_id:
            return RedirectResponse("/posting", status_code=303)

        rules = [r for r in await db.list_posting_rules(session, user_id) if r.active]
        photos = await db.list_photos(session, user_id)
        if not rules or not photos:
            return RedirectResponse("/posting", status_code=303)

        styles_by_id = {s.id: s for s in await db.list_styles(session, user_id)}

        # Calcule chaque occurrence (règle × semaine) à venir, triée dans le temps,
        # pour que le tirage des photos avance de façon cohérente sur tout le planning.
        now = datetime.datetime.utcnow()
        occurrences = []
        for rule in rules:
            style = styles_by_id.get(rule.style_id)
            if not style:
                continue
            hour, minute = (int(p) for p in rule.time.split(":"))
            for week in range(weeks_ahead):
                days_until = (rule.day_of_week - now.weekday()) % 7 + week * 7
                occurrence_date = (now + datetime.timedelta(days=days_until)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if occurrence_date <= now:
                    occurrence_date += datetime.timedelta(days=7)
                occurrences.append((occurrence_date, rule, style))
        occurrences.sort(key=lambda o: o[0])

        pool = photos.copy()
        random.shuffle(pool)
        cursor = [0]

        content_ids = []
        for occurrence_date, rule, style in occurrences:
            picked = _draw_photos(pool, cursor, style.photo_count)
            item = db.GeneratedContent(
                user_id=user_id,
                style_id=style.id,
                photo_urls=[p.url for p in picked],
                account_ids=rule.account_ids,
                scheduled_for=occurrence_date,
                status="pending",
            )
            session.add(item)
            await session.flush()
            content_ids.append(item.id)
        await session.commit()

    background_tasks.add_task(_run_batch, user_id, content_ids)
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


# ─── Facturation (Stripe) ────────────────────────────────────────────────────
@app.post("/billing/checkout")
async def billing_checkout(request: Request, plan: str = Form(...)):
    user_id = _session_user_id(request)
    if not user_id or plan not in billing.PLANS:
        return RedirectResponse("/", status_code=303)

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user:
            return RedirectResponse("/", status_code=303)

        base = _base_url(request)
        checkout_url = billing.create_checkout_session(
            plan=plan,
            customer_id=user.stripe_customer_id,
            customer_email=user.email,
            success_url=f"{base}/settings?checkout=success",
            cancel_url=f"{base}/settings?checkout=cancelled",
        )
    return RedirectResponse(checkout_url, status_code=303)


@app.get("/billing/portal")
async def billing_portal(request: Request):
    user_id = _session_user_id(request)
    if not user_id:
        return RedirectResponse("/")

    async with db.get_session() as session:
        user = await db.get_user(session, user_id)
        if not user or not user.stripe_customer_id:
            return RedirectResponse("/settings")

        portal_url = billing.create_portal_session(
            customer_id=user.stripe_customer_id,
            return_url=f"{_base_url(request)}/settings",
        )
    return RedirectResponse(portal_url, status_code=303)


@app.post("/admin/grant-access")
async def admin_grant_access(request: Request, secret: str = Form(...), email: str = Form(...), plan: str = Form("pro")):
    """
    Débloque manuellement un abonnement (test/support), sans passer par Stripe.
    Protégé par ADMIN_SECRET — à retirer ou sécuriser davantage avant un vrai lancement public.
    """
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return HTMLResponse("Forbidden", status_code=403)

    async with db.get_session() as session:
        user = await db.get_user_by_email(session, email)
        if not user:
            return HTMLResponse(f"Aucun compte trouvé pour {email}", status_code=404)
        user.plan = plan
        user.subscription_status = "active"
        await session.commit()

    return {"email": email, "plan": plan, "subscription_status": "active"}


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = billing.construct_webhook_event(payload, sig_header)
    except Exception as e:
        return HTMLResponse(f"Invalid webhook: {e}", status_code=400)

    async with db.get_session() as session:
        if event["type"] == "checkout.session.completed":
            checkout_session = event["data"]["object"]
            customer_id = checkout_session.get("customer")
            customer_email = (checkout_session.get("customer_details") or {}).get("email")

            user = None
            if customer_id:
                user = await db.get_user_by_stripe_customer(session, customer_id)
            if not user and customer_email:
                user = await db.get_user_by_email(session, customer_email)

            if user:
                user.stripe_customer_id = customer_id
                user.stripe_subscription_id = checkout_session.get("subscription")
                user.subscription_status = "active"
                plan = (checkout_session.get("metadata") or {}).get("plan")
                if plan:
                    user.plan = plan
                await session.commit()

        elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
            sub = event["data"]["object"]
            user = await db.get_user_by_stripe_customer(session, sub.get("customer"))
            if user:
                plan = (sub.get("metadata") or {}).get("plan")
                if not plan:
                    price_id = sub["items"]["data"][0]["price"]["id"] if sub.get("items", {}).get("data") else None
                    plan = billing.plan_from_price_id(price_id) if price_id else None
                if plan:
                    user.plan = plan
                user.subscription_status = "active" if sub.get("status") == "active" else sub.get("status", "inactive")
                await session.commit()

    return {"received": True}


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
