#!/usr/bin/env python3
"""
Script ponctuel : crée les produits/tarifs ContentAI Studio dans Stripe
(mode test tant que STRIPE_SECRET_KEY commence par sk_test_).

Lance-le une seule fois :
    cd ~/contentai-studio-backend && source venv/bin/activate && python3 setup_stripe.py

Il affiche les Price IDs à copier dans .env (STRIPE_PRICE_*).
"""
import os

import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]


def create_plan(name: str, amount_cents: int) -> str:
    product = stripe.Product.create(name=f"ContentAI Studio — {name}")
    price = stripe.Price.create(
        product=product.id,
        unit_amount=amount_cents,
        currency="eur",
        recurring={"interval": "month"},
    )
    print(f"{name:12s} -> price {price.id} ({amount_cents/100:.2f} €/mois)")
    return price.id


def main():
    print(f"Mode : {'TEST' if stripe.api_key.startswith('sk_test_') else 'LIVE ⚠️'}\n")

    starter_price = create_plan("Starter", 2999)
    pro_price = create_plan("Pro", 5999)
    business_price = create_plan("Business", 14999)

    coupon = stripe.Coupon.create(
        name="Premier mois Starter",
        amount_off=2000,  # 20,00 € de réduction
        currency="eur",
        duration="once",
    )
    print(f"{'Coupon':12s} -> {coupon.id} (-20,00 € une fois, pour ramener Starter à 9,99 € le 1er mois)")

    print("\n--- À coller dans .env ---")
    print(f"STRIPE_PRICE_STARTER={starter_price}")
    print(f"STRIPE_PRICE_PRO={pro_price}")
    print(f"STRIPE_PRICE_BUSINESS={business_price}")
    print(f"STRIPE_COUPON_STARTER_FIRST_MONTH={coupon.id}")


if __name__ == "__main__":
    main()
