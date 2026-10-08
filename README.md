# Pulse

A calendar for courses and important mail. It runs on a computer you control. You press Scan when you want an update.

Sign-in is a Chrome window on that computer. If Canvas, CalCentral, or mail asks for a text message or Duo, you tap it yourself. Pulse does not see the code and does not store the password. After you save the sign-in, a scan reads Canvas as JSON and a syllabus page as text. It does not take screenshots.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pulse serve
```

Open http://127.0.0.1:8787, or double-click `Pulse.command`. That launcher is the app. The Python environment stays behind it.

Set `PULSE_POSTHOG_KEY` to the project key (`phc_`) when you want traffic in PostHog. A secret key (`phs_`) is ignored, so it never lands in the page. Page views, Scan, and Connect clicks are counted. Session replay stays off, so course text and mail are not recorded. See `.env.example`.

`pulse products` creates or updates the Lock In Cal product on the Stripe account for `STRIPE_SECRET_KEY`. Run it again whenever the catalog changes. It does not create a price, and the Subscribe button stays hidden. Prefer a restricted key with Products write permission. Use `rk_live_` for the live account.

Billing stays off until `STRIPE_BILLING=1` and `STRIPE_PRICE_ID` are both set. Then Subscribe uses Stripe Checkout, and Manage plan opens the Customer Portal. Access turns on only after Stripe reports the payment as paid. Forward webhooks while developing:

```bash
stripe listen --forward-to localhost:8787/billing/webhook \
  --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,invoice.paid,invoice.payment_failed,customer.subscription.updated,customer.subscription.deleted,charge.dispute.created,charge.refunded,radar.early_fraud_warning.created
```

Put the `whsec_` value from that command in `STRIPE_WEBHOOK_SECRET`. Tax stays off until a price is actually sold. When billing is turned on, confirm Smart Retries under Billing → Revenue recovery in the Stripe Dashboard.

Connect bCourses, CalCentral, and mail from **Connect**. You finish the text or Duo prompt yourself. Pulse keeps those keys in `data/` on this computer and does not store the password. Mail can use that saved sign-in. An IMAP app password in Settings is optional.

A sample semester is on the home page if you want to click through before connecting anything. Remove it before a real scan.

`pulse sync` scans once from the terminal. The server does not scan in the background.

Secrets go in `data/config.json` (mode 600) or in environment variables. The saved browser sign-in is `data/session.json`. See `.env.example`.

## What a scan costs

| Kind | What | How | Model |
| --- | --- | --- | --- |
| Live | Assignments, due dates | Canvas JSON, using the saved sign-in or a token | None |
| Live | Mail | Saved Gmail sign-in, or IMAP if you set an app password | None |
| Static | Syllabus, grading, late policy, drop rules | One parse per changed document, then cached | One call, and only if you add an API key |
| Either | Questions | Search the cached sentences and quote the line | Optional, and only over those lines |
| Live | Study blocks | Plain scheduling code | None |

The footer counts parses, static cache hits, and model calls.

## On another computer

`pulse serve` opens the sign-in window on the computer where the server is running. Use that computer, or a machine with a screen, when a prompt is likely. Docker publishes the calendar on this laptop only:

```bash
docker compose up --build
```

The container has no screen, so the click-through window stays on a computer where Chrome can open. On a VPS, set `PULSE_AUTH_TOKEN` before listening beyond localhost. `pulse serve --host 0.0.0.0` refuses to start until that token, or `PULSE_ALLOW_OPEN=1`, is set.

## Tests

```bash
pytest
```
