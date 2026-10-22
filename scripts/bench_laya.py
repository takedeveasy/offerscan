"""Times the Laya engine on stored offers: one request per offer vs. batched requests.

Usage: python scripts/bench_laya.py [--db data/offerscan.sqlite] [-n 32]   (inside the project venv)
Needs a running Laya server (see scripts/laya.ps1). Reads offers only; writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

from offerscan.ai.laya import make_laya_engine
from offerscan.ai.questions import OFFER_QUESTIONS_V1, build_offer_state
from offerscan.settings import Settings
from offerscan.storage.db import Database


async def run(db_path: Path, count: int) -> dict:
    settings = Settings()
    offers = [o for o in Database(db_path).all_offers() if len(o.description_md) > 200][:count]
    if not offers:
        raise SystemExit("No hay ofertas con descripcion en la base de datos.")
    states = [build_offer_state(o) for o in offers]
    health = httpx.get(f"{settings.laya_base_url}/health", timeout=5).json()
    out: dict = {"offers": len(offers), "device": health.get("device"), "single_s": None, "batch_s": None}

    engine = make_laya_engine(settings)
    try:
        await engine.preflight()
        await engine.ask(states[0], OFFER_QUESTIONS_V1)  # warm-up
        t0 = time.perf_counter()
        for state in states:
            await engine.ask(state, OFFER_QUESTIONS_V1)
        out["single_s"] = round(time.perf_counter() - t0, 2)
        if hasattr(engine, "ask_batch"):
            await engine.ask_batch(states[:2], OFFER_QUESTIONS_V1)  # warm-up
            t0 = time.perf_counter()
            await engine.ask_batch(states, OFFER_QUESTIONS_V1)
            out["batch_s"] = round(time.perf_counter() - t0, 2)
    finally:
        await engine.aclose()
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/offerscan.sqlite"))
    parser.add_argument("-n", type=int, default=32)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.db, args.n)), indent=2))


if __name__ == "__main__":
    main()
