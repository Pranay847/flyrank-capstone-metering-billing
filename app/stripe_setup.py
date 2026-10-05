"""One-time helper: create the Pro product + $29/month price in Stripe TEST mode.

    python -m app.stripe_setup

Idempotent: looks the price up by lookup_key first, so running it twice never
creates two prices. Refuses to run with a live key. Prints the price id to put in
.env as STRIPE_PRICE_PRO.
"""

from __future__ import annotations

import sys

import stripe

from .config import get_settings

LOOKUP_KEY = "flyrank_pro_monthly"


def main() -> int:
    key = get_settings().stripe_secret_key
    if not key.startswith("sk_test_"):
        print("Refusing to run: STRIPE_SECRET_KEY must be a TEST key (sk_test_...).")
        return 1
    existing = stripe.Price.list(api_key=key, lookup_keys=[LOOKUP_KEY], limit=1)
    if existing.data:
        price = existing.data[0]
        print(f"Pro price already exists: {price.id}")
    else:
        product = stripe.Product.create(api_key=key, name="Metering Engine — Pro")
        price = stripe.Price.create(
            api_key=key, product=product.id, unit_amount=2900, currency="usd",
            recurring={"interval": "month"}, lookup_key=LOOKUP_KEY,
        )
        print(f"Created Pro price: {price.id}")
    print(f"\nAdd this to .env:\nSTRIPE_PRICE_PRO={price.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
