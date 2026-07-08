"""
Facturation — abonnements via Stripe Checkout + Customer Portal.

Stripe héberge la page de paiement et le formulaire de gestion d'abonnement :
on ne manipule jamais de numéro de carte nous-mêmes. Un webhook nous notifie
des changements (paiement réussi, annulation, etc.) pour tenir à jour le
statut d'abonnement de chaque client Studio.
"""
import os

import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

PLANS = {
    "starter": {
        "label": "Starter",
        "price_id_env": "STRIPE_PRICE_STARTER",
        "coupon_env": "STRIPE_COUPON_STARTER_FIRST_MONTH",  # 1er mois à prix réduit
        "price_display": "9,99 € le 1er mois, puis 29,99 €/mois",
    },
    "pro": {
        "label": "Pro",
        "price_id_env": "STRIPE_PRICE_PRO",
        "coupon_env": None,
        "price_display": "59,99 €/mois",
    },
    # "business" retiré temporairement de l'offre (le produit/prix Stripe existe
    # toujours, STRIPE_PRICE_BUSINESS — on peut le remettre plus tard).
}


def _cfg(name: str) -> str:
    return os.environ.get(name, "")


def create_checkout_session(
    plan: str,
    customer_id: str | None,
    customer_email: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Crée une session Stripe Checkout (abonnement) et renvoie son URL."""
    plan_cfg = PLANS[plan]
    price_id = _cfg(plan_cfg["price_id_env"])

    params = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"plan": plan},
        "subscription_data": {"metadata": {"plan": plan}},
    }
    if customer_id:
        params["customer"] = customer_id
    else:
        params["customer_email"] = customer_email

    coupon_id = _cfg(plan_cfg["coupon_env"]) if plan_cfg["coupon_env"] else None
    if coupon_id:
        params["discounts"] = [{"coupon": coupon_id}]
    else:
        params["allow_promotion_codes"] = True

    session = stripe.checkout.Session.create(**params)
    return session.url


def create_portal_session(customer_id: str, return_url: str) -> str:
    """Crée une session du Customer Portal Stripe (gestion/annulation d'abonnement)."""
    session = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return session.url


def construct_webhook_event(payload: bytes, sig_header: str):
    """Vérifie la signature du webhook et renvoie l'événement Stripe."""
    webhook_secret = _cfg("STRIPE_WEBHOOK_SECRET")
    return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)


def plan_from_price_id(price_id: str) -> str | None:
    for plan, cfg in PLANS.items():
        if _cfg(cfg["price_id_env"]) == price_id:
            return plan
    return None
