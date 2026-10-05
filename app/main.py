"""Command-line entry point. One codebase, several roles, so each can be
deployed and scaled independently:

  python -m app.main migrate          apply database migrations
  python -m app.main api              HTTP API only
  python -m app.main worker           payment processing only
  python -m app.main dispatcher       webhook delivery only
  python -m app.main all              migrate + api + worker + dispatcher (local development)
  python -m app.main mock-bank        the sandbox bank
  python -m app.main create-api-key --customer C12345 --name "Acme Corp"
"""
import argparse
import logging
import signal
import threading

import uvicorn

from .auth import create_customer_with_api_key
from .config import Settings
from .db import create_pool, migrate
from .logs import configure_logging

log = logging.getLogger("ach.main")


def main() -> None:
    parser = argparse.ArgumentParser(description="ACH payment platform")
    parser.add_argument("command", choices=["migrate", "api", "worker", "dispatcher", "all", "mock-bank", "create-api-key"])
    parser.add_argument("--customer")
    parser.add_argument("--name")
    args = parser.parse_args()

    settings = Settings()
    configure_logging(settings.log_level)

    if args.command == "mock-bank":
        from mock_bank.server import MockBank
        bank = MockBank(host=settings.host, port=settings.mock_bank_port, failure_rate=settings.mock_bank_failure_rate)
        log.info("mock bank listening", extra={"port": bank.port, "failure_rate": settings.mock_bank_failure_rate})
        bank.server.serve_forever()
        return

    if args.command in ("migrate", "create-api-key", "all"):
        pool = create_pool(settings.database_url, 2)
        try:
            if args.command == "create-api-key":
                if not args.customer:
                    parser.error("create-api-key needs --customer")
                key = create_customer_with_api_key(pool, args.customer, args.name or args.customer)
                print(f"\nAPI key for {args.customer} (shown once, store it safely):\n\n  {key['api_key']}\n")
                return
            applied = migrate(pool)
            log.info("migrations applied" if applied else "database already up to date", extra={"applied": applied})
        finally:
            pool.close()
        if args.command == "migrate":
            return

    from .platform import Platform
    platform = Platform(settings)
    if args.command in ("worker", "all"):
        platform.worker.start()
    if args.command in ("dispatcher", "all"):
        platform.dispatcher.start()
    log.info("ach payment platform started", extra={"role": args.command})

    try:
        if args.command in ("api", "all"):
            # uvicorn handles Ctrl+C / SIGTERM and returns when the server stops.
            uvicorn.run(platform.api, host=settings.host, port=settings.port, log_config=None)
        else:
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            stop.wait()
    finally:
        log.info("shutting down gracefully")
        platform.close()


if __name__ == "__main__":
    main()
