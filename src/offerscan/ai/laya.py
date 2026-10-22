"""Hybrid engine: Laya (free, local) answers the questions it handles well; heuristics answer the rest."""

from __future__ import annotations

from typing import Any

from offerscan.ai.heuristic import HeuristicEngine
from offerscan.ai.jev_client import JevClient, JevResult, JevUnreachable
from offerscan.ai.questions import LAYA_OFFER_QUESTIONS_V1, LAYA_OFFER_VERSION
from offerscan.settings import Settings

UNREACHABLE_MSG = (
    "No se puede conectar con el servidor Laya en {url}. Arranca el motor local con "
    ".\\scripts\\laya.ps1 o bash scripts/laya.sh (con --install la primera vez) y vuelve a intentarlo."
)


class LayaUnavailable(JevUnreachable):
    """Laya server is down; carries a Spanish message for the user."""


def junior_friendly_from(junior_target: dict[str, Any]) -> float:
    probs = junior_target.get("probabilities") or {}
    if "junior" in probs:
        return float(probs["junior"])
    return 1.0 if junior_target.get("choice") == "junior" else 0.0


class LayaHybridEngine:
    engine_name = "laya"
    offer_version = LAYA_OFFER_VERSION
    batch_size = 64  # laya-serve MAX_BATCH_STATES

    def __init__(self, client: JevClient):
        self._client = client
        self._heuristic = HeuristicEngine()
        self.model = f"laya:{client.model}"

    async def preflight(self) -> None:
        if not await self._client.health():
            raise LayaUnavailable(UNREACHABLE_MSG.format(url=self._client.base_url))

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> JevResult:
        result = await self._heuristic.ask(state, questions)
        if isinstance(state, dict) and "candidate" in state:  # match questions stay heuristic
            return result
        wanted = self._wanted(questions)
        try:
            laya = await self._client.ask(state, wanted)
        except JevUnreachable as exc:
            raise LayaUnavailable(UNREACHABLE_MSG.format(url=self._client.base_url)) from exc
        return self._merge(result, laya, questions)

    async def ask_batch(self, states: list[Any], questions: dict[str, dict[str, Any]]) -> list[JevResult]:
        """Offer states only: one `/v1/systemone/batch` call (<= BATCH_SIZE states) instead of one call each."""
        results = [await self._heuristic.ask(s, questions) for s in states]
        try:
            layas = await self._client.ask_batch(states, self._wanted(questions))
        except JevUnreachable as exc:
            raise LayaUnavailable(UNREACHABLE_MSG.format(url=self._client.base_url)) from exc
        return [self._merge(r, laya, questions) for r, laya in zip(results, layas, strict=True)]

    @staticmethod
    def _wanted(questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in LAYA_OFFER_QUESTIONS_V1.items() if k in questions or k == "junior_target"}

    @staticmethod
    def _merge(result: JevResult, laya: JevResult, questions: dict[str, dict[str, Any]]) -> JevResult:
        for qid, answer in laya.answers.items():
            if qid in questions:
                result.answers[qid] = answer
        target = laya.answers["junior_target"]
        result.answers["junior_target"] = target
        if "junior_friendly" in questions:
            friendly = junior_friendly_from(target)
            result.answers["junior_friendly"] = {
                "type": "noul",
                "noul": round(friendly, 3),
                "probabilities": {},
                "confidence": target.get("confidence"),
            }
        result.usage = laya.usage
        result.elapsed_ms = laya.elapsed_ms
        return result

    async def aclose(self) -> None:
        await self._client.aclose()


def make_laya_engine(settings: Settings) -> LayaHybridEngine:
    key = settings.laya_api_key.get_secret_value() if settings.laya_api_key else ""
    client = JevClient(
        key,
        settings.laya_base_url,
        settings.laya_model,
        max_credits=float("inf"),
        concurrency=1,
        timeout=900.0,  # a 64-offer batch on CPU can take minutes
        extra_body={"max_len": settings.laya_max_len},
        fail_fast_connect=True,
        engine_name="laya",
    )
    return LayaHybridEngine(client)
