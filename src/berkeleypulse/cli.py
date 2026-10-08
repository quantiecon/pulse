from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from berkeleypulse.app import create_app
from berkeleypulse.billing import BillingError, push_catalog
from berkeleypulse.db import db
from berkeleypulse.demo import seed_demo
from berkeleypulse.notify import notify
from berkeleypulse.qa import answer_question
from berkeleypulse.config import load_settings
from berkeleypulse.sync import sync_all


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pulse", description="Calendar for courses and important mail")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the local web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    sub.add_parser("sync", help="Scan Canvas and mail once")
    sub.add_parser("products", help="Push the product catalog to Stripe")
    sub.add_parser("demo", help="Load the demo semester")

    ask = sub.add_parser("ask", help="Ask a question against ingested course documents")
    ask.add_argument("question")
    ask.add_argument("--course", default="")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "serve":
        if args.host not in LOCAL_HOSTS:
            token = os.environ.get("PULSE_AUTH_TOKEN", "")
            allow = os.environ.get("PULSE_ALLOW_OPEN") == "1"
            if not token and not allow:
                print(
                    "Refusing to listen beyond localhost without PULSE_AUTH_TOKEN.\n"
                    "Set PULSE_AUTH_TOKEN, or PULSE_ALLOW_OPEN=1 only when a firewall already limits access.",
                    file=sys.stderr,
                )
                return 2
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0

    if args.cmd == "products":
        load_settings()
        try:
            for line in push_catalog():
                print(line)
        except BillingError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    if args.cmd == "sync":
        result = sync_all()
        notify(result.new_urgent)
        print(result.message)
        return 0

    if args.cmd == "demo":
        created = seed_demo()
        print("Demo semester loaded." if created else "Demo semester is already loaded.")
        return 0

    course_id = int(args.course) if str(args.course).isdigit() else None
    with db() as conn:
        answer = answer_question(conn, args.question, load_settings(), course_id)
    print(answer.text)
    for citation in answer.citations:
        print("- %s, %s: %s" % (citation.source, citation.locator, citation.quote))
    return 0
