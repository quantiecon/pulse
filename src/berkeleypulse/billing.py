from __future__ import annotations

import os
from datetime import datetime, timezone

from stripe import InvalidRequestError, SignatureVerificationError, StripeClient, StripeError

from berkeleypulse.db import db

# Stable label so Checkout sessions from this app group together in Stripe.
INTEGRATION_IDENTIFIER = "lockincal_checkout_mhrvkbwn"
ACTIVE = {"active", "trialing"}

# Catalog only. Prices stay off until billing is turned on.
CATALOG = (
    {
        "id": "lockincal",
        "name": "Lock In Cal",
        "description": "Smart calendar for students.",
    },
)


class BillingError(Exception):
    pass


def _secret() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def _price_id() -> str:
    return os.environ.get("STRIPE_PRICE_ID", "").strip()


def _portal_configuration() -> str:
    return os.environ.get("STRIPE_PORTAL_CONFIGURATION", "").strip()


def _webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def _client() -> StripeClient:
    key = _secret()
    if not key:
        raise BillingError("Add STRIPE_SECRET_KEY to .env. A restricted rk_live_ or rk_test_ key is enough.")
    return StripeClient(key)


def _selling() -> bool:
    return os.environ.get("STRIPE_BILLING", "").strip() == "1"


def _livemode(key: str) -> bool:
    return key.startswith("rk_live_") or key.startswith("sk_live_")


def billing_state() -> dict:
    row = _load()
    return {
        "configured": _selling() and bool(_secret() and _price_id()),
        "active": row["status"] in ACTIVE,
        "status": row["status"],
        "manage": _selling() and bool(row["customer_id"]),
    }


def push_catalog() -> list:
    """Create or update catalog products. Does not create prices."""
    client = _client()
    lines = ["live account" if _livemode(_secret()) else "test account"]
    for product in CATALOG:
        params = {
            "name": product["name"],
            "description": product["description"],
            "metadata": {"app": "pulse", "site": "lockincal.com"},
        }
        try:
            client.v1.products.retrieve(product["id"])
        except InvalidRequestError as exc:
            if getattr(exc, "code", "") != "resource_missing":
                raise BillingError("Stripe could not read the product catalog.")
            try:
                client.v1.products.create({"id": product["id"], **params})
            except StripeError:
                raise BillingError("Stripe could not create %s." % product["name"])
            lines.append("created %s" % product["name"])
            continue
        except StripeError:
            raise BillingError("Stripe could not read the product catalog.")
        try:
            client.v1.products.update(product["id"], params)
        except StripeError:
            raise BillingError("Stripe could not update %s." % product["name"])
        lines.append("updated %s" % product["name"])
    return lines


def checkout_params(origin: str, customer_id: str, price: str) -> dict:
    root = origin.rstrip("/")
    params = {
        "mode": "subscription",
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": root + "/billing/return?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": root + "/",
        "integration_identifier": INTEGRATION_IDENTIFIER,
        "client_reference_id": "pulse",
        "metadata": {"app": "pulse", "site": "lockincal.com"},
        "subscription_data": {
            "billing_mode": {"type": "flexible"},
            "metadata": {"app": "pulse"},
        },
    }
    if customer_id:
        params["customer"] = customer_id
    return params


def portal_params(origin: str, customer_id: str, configuration: str) -> dict:
    params = {"customer": customer_id, "return_url": origin.rstrip("/") + "/"}
    if configuration:
        params["configuration"] = configuration
    return params


def checkout_url(origin: str) -> str:
    if not _selling():
        raise BillingError("Billing is off.")
    row = _load()
    if row["status"] in ACTIVE and row["customer_id"]:
        return portal_url(origin)
    price = _price_id()
    if not price:
        raise BillingError("Add STRIPE_PRICE_ID to .env.")
    try:
        session = _client().v1.checkout.sessions.create(
            checkout_params(origin, row["customer_id"], price)
        )
    except StripeError:
        raise BillingError("Stripe could not start Checkout.")
    url = str(getattr(session, "url", "") or "")
    if not url:
        raise BillingError("Stripe did not return a Checkout link.")
    return url


def portal_url(origin: str) -> str:
    row = _load()
    if not row["customer_id"]:
        raise BillingError("Subscribe before opening the billing portal.")
    try:
        session = _client().v1.billing_portal.sessions.create(
            portal_params(origin, row["customer_id"], _portal_configuration())
        )
    except StripeError:
        raise BillingError("Stripe could not open the billing portal.")
    url = str(getattr(session, "url", "") or "")
    if not url:
        raise BillingError("Stripe did not return a billing portal link.")
    return url


def sync_session(session_id: str) -> str:
    if not session_id.startswith("cs_") or len(session_id) > 255:
        raise BillingError("That Checkout session is not valid.")
    try:
        session = _client().v1.checkout.sessions.retrieve(session_id)
    except StripeError:
        raise BillingError("Stripe could not load that Checkout session.")
    payment = str(getattr(session, "payment_status", "") or "")
    _apply_checkout(_as_dict(session))
    return payment


def handle_webhook(payload: bytes, signature: str) -> None:
    secret = _webhook_secret()
    if not secret:
        raise BillingError("Add STRIPE_WEBHOOK_SECRET to .env.")
    try:
        event = _client().construct_event(payload, signature, secret)
    except SignatureVerificationError:
        raise BillingError("Stripe signature did not match.")
    except ValueError:
        raise BillingError("Stripe event was not valid JSON.")
    apply_event(_as_dict(event))


def apply_event(event: dict) -> None:
    kind = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return
    if kind in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        _apply_checkout(obj)
        return
    if kind == "checkout.session.async_payment_failed":
        _save(
            customer_id=_ref(obj.get("customer")),
            subscription_id=_ref(obj.get("subscription")),
            status="past_due",
            note=kind,
        )
        return
    if kind == "invoice.paid":
        _save(
            customer_id=_ref(obj.get("customer")),
            subscription_id=_invoice_subscription(obj),
            status="active",
            note=kind,
        )
        return
    if kind == "invoice.payment_failed":
        _save(
            customer_id=_ref(obj.get("customer")),
            subscription_id=_invoice_subscription(obj),
            status="past_due",
            note=kind,
        )
        return
    if kind == "customer.subscription.updated":
        _save(
            customer_id=_ref(obj.get("customer")),
            subscription_id=_ref(obj.get("id")),
            status=str(obj.get("status") or ""),
            price_id=_price_from_subscription(obj),
            note=kind,
        )
        return
    if kind == "customer.subscription.deleted":
        _save(
            customer_id=_ref(obj.get("customer")),
            subscription_id=_ref(obj.get("id")),
            status="canceled",
            price_id=_price_from_subscription(obj),
            note=kind,
        )
        return
    if kind in {"charge.dispute.created", "radar.early_fraud_warning.created"}:
        _save(customer_id=_ref(obj.get("customer")), status="review", note=kind)
        return
    if kind == "charge.refunded":
        _save(customer_id=_ref(obj.get("customer")), note=kind)


def _apply_checkout(session: dict) -> None:
    payment = str(session.get("payment_status") or "")
    status = "active" if payment in {"paid", "no_payment_required"} else "incomplete"
    _save(
        customer_id=_ref(session.get("customer")),
        subscription_id=_ref(session.get("subscription")),
        status=status,
        price_id=_price_id(),
        note=payment or "checkout",
    )


def _invoice_subscription(invoice: dict) -> str:
    found = _ref(invoice.get("subscription"))
    if found:
        return found
    parent = invoice.get("parent") or {}
    if not isinstance(parent, dict):
        return ""
    details = parent.get("subscription_details") or {}
    if not isinstance(details, dict):
        return ""
    return _ref(details.get("subscription"))


def _price_from_subscription(subscription: dict) -> str:
    items = subscription.get("items") or {}
    rows = items.get("data") if isinstance(items, dict) else None
    if not rows:
        return ""
    price = rows[0].get("price") if isinstance(rows[0], dict) else None
    return _ref(price)


def _ref(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return ""


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    raw = getattr(value, "to_dict", None)
    if callable(raw):
        data = raw()
        if isinstance(data, dict):
            return data
    return {}


def _load() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT customer_id, subscription_id, status, price_id, note FROM billing WHERE id = 1"
        ).fetchone()
    if row is None:
        return {"customer_id": "", "subscription_id": "", "status": "", "price_id": "", "note": ""}
    return {key: str(row[key] or "") for key in ("customer_id", "subscription_id", "status", "price_id", "note")}


def _save(
    customer_id: str = "",
    subscription_id: str = "",
    status: str = "",
    price_id: str = "",
    note: str = "",
) -> None:
    current = _load()
    updated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO billing (id, customer_id, subscription_id, status, price_id, note, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              customer_id = excluded.customer_id,
              subscription_id = excluded.subscription_id,
              status = excluded.status,
              price_id = excluded.price_id,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (
                customer_id or current["customer_id"],
                subscription_id or current["subscription_id"],
                status or current["status"],
                price_id or current["price_id"],
                note,
                updated,
            ),
        )
