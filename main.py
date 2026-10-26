"""Entry point: python main.py  (Render start command). Paper trading only."""
import logging, os, sys

from aiohttp import web

from copytrader.app import build_context, make_app
from copytrader.util import load_config


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not os.environ.get("HELIUS_API_KEY"):
        logging.warning("HELIUS_API_KEY not set: scouting and watching will fail until it is.")
    ctx = build_context(cfg)
    web.run_app(make_app(ctx), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
