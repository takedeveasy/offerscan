from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("offerscan.jev")

RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class JevError(Exception):
    """Base class for Jev client errors."""


class JevAuthError(JevError):
    """HTTP 401: invalid or missing API key."""


class JevCreditsError(JevError):
    """HTTP 402: no credits left."""


class JevRequestError(JevError):
    """HTTP 400/422 or other non-retryable client error."""


class JevInvalidResponse(JevError):
    """2xx response that is not a valid answers envelope."""


class JevTransientError(JevError):
    """Retries exhausted (timeout, 429 or 5xx)."""


class JevUnreachable(JevError):
    """The server cannot be reached at all (used to fail fast for the local Laya server)."""


class JevBudgetExceeded(JevError):
    """JEV_MAX_CREDITS_PER_RUN reached."""


@dataclass
class JevResult:
    answers: dict[str, dict[str, Any]]
    credits_used: float = 0
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float | None = None


def build_request_body(model: str, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The only place that knows the request schema of POST /v1/systemone."""
    body_questions: dict[str, dict[str, Any]] = {}
    for qid, q in questions.items():
        item: dict[str, Any] = {"type": q["type"], "instructions": q["instructions"]}
        if q.get("criteria") is not None:
            item["criteria"] = q["criteria"]
        body_questions[qid] = item
    return {"model": model, "state": state, "questions": body_questions}


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_answer(qtype: str, raw: dict[str, Any], question: dict[str, Any] | None) -> dict[str, Any]:
    """Map one raw answer to the canonical shape shared with the heuristic engine.

    choice: {type, choice, probabilities, confidence}
    score:  {type, score (label), index (0 = lowest level), probabilities, confidence}
    noul:   {type, noul (0..1)}
    """
    qtype = raw.get("type", qtype)
    out: dict[str, Any] = {"type": qtype}
    probs = raw.get("probabilities") or raw.get("probs") or {}
    if isinstance(probs, list):
        probs = {str(i): p for i, p in enumerate(probs)}
    out["probabilities"] = probs if isinstance(probs, dict) else {}
    # Laya's own `confidence` is entropy-based; `x_jev_confidence` is the Jev-comparable one.
    xc = _float(raw.get("x_jev_confidence"))
    out["confidence"] = xc if xc is not None else _float(raw.get("confidence"))
    if qtype == "noul":
        value = _float(raw.get("noul", raw.get("probability", raw.get("value"))))
        if value is None:
            raise JevInvalidResponse("noul answer without numeric value")
        out["noul"] = min(1.0, max(0.0, value))
        return out
    if qtype == "choice":
        choice = raw.get("choice", raw.get("answer", raw.get("value")))
        if choice is None:
            raise JevInvalidResponse("choice answer without value")
        out["choice"] = str(choice)
        return out
    if qtype == "score":
        scale = [str(x) for x in (question or {}).get("criteria") or []]
        legend = raw.get("legend")
        if not scale and isinstance(legend, dict):
            scale = [str(legend[k]) for k in sorted(legend, key=lambda k: int(k) if str(k).isdigit() else 0)]
        value = raw.get("score", raw.get("answer", raw.get("value")))
        label, index = _score_label_index(value, scale)
        out.update(score=label, index=index)
        return out
    raise JevInvalidResponse(f"unknown answer type {qtype!r}")


def _score_label_index(value: Any, scale: list[str]) -> tuple[str, float]:
    if value is None:
        raise JevInvalidResponse("score answer without value")
    if isinstance(value, str) and value in scale:
        return value, float(scale.index(value))
    number = _float(value)
    if number is None:
        lowered = {s.lower(): i for i, s in enumerate(scale)}
        if str(value).lower() in lowered:
            idx = lowered[str(value).lower()]
            return scale[idx], float(idx)
        raise JevInvalidResponse(f"score {value!r} not in scale")
    top = max(len(scale) - 1, 0)
    index = min(max(number, 0.0), float(top)) if scale else number
    label = scale[round(index)] if scale else str(value)
    return label, index


def parse_response(payload: Any, questions: dict[str, dict[str, Any]]) -> JevResult:
    """Validate a Jev envelope `{code, data: {result: {answers}, creditsUsed}}` or a bare Laya body."""
    if not isinstance(payload, dict):
        raise JevInvalidResponse("response is not a JSON object")
    if "answers" in payload and "data" not in payload:  # bare Laya shape
        answers, usage, elapsed, credits = payload["answers"], payload.get("usage"), None, 0.0
    else:
        if payload.get("code", 0) != 0:
            raise JevInvalidResponse(f"code={payload.get('code')} message={payload.get('message')}")
        data = payload.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        answers = result.get("answers") if isinstance(result, dict) else None
        usage = result.get("usage") if isinstance(result, dict) else None
        elapsed = _float(result.get("elapsedMs")) if isinstance(result, dict) else None
        credits = (_float(data.get("creditsUsed")) if isinstance(data, dict) else None) or 0.0
    if not isinstance(answers, dict) or not answers:
        raise JevInvalidResponse("missing answers")
    normalized: dict[str, dict[str, Any]] = {}
    for qid, question in questions.items():
        if qid not in answers:
            raise JevInvalidResponse(f"answer for {qid!r} missing")
        normalized[qid] = normalize_answer(question["type"], answers[qid], question)
    return JevResult(
        answers=normalized,
        credits_used=credits,
        usage=usage if isinstance(usage, dict) else {},
        elapsed_ms=elapsed,
    )


class JevClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://thejevai.com",
        model: str = "typesafe/jev-1.13",
        *,
        max_credits: float = 500,
        concurrency: int = 4,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        extra_body: dict[str, Any] | None = None,
        fail_fast_connect: bool = False,
        engine_name: str = "jev",
    ):
        self.model = model
        self.max_credits = max_credits
        self.credits_used = 0.0
        self.requests_made = 0
        self._url = f"{base_url.rstrip('/')}/v1/systemone"
        self._batch_url = f"{self._url}/batch"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._extra_body = extra_body or {}
        self._fail_fast_connect = fail_fast_connect
        self.engine_name = engine_name
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._http = httpx.AsyncClient(timeout=timeout)

    def __repr__(self) -> str:
        return f"JevClient(model={self.model!r}, url={self._url!r})"

    async def health(self) -> bool:
        try:
            return (await self._http.get(f"{self.base_url}/health", timeout=5)).status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> JevResult:
        if self.credits_used >= self.max_credits:
            raise JevBudgetExceeded(f"credit budget {self.max_credits} reached")
        body = {**build_request_body(self.model, state, questions), **self._extra_body}
        async with self._semaphore:
            payload = await self._post_with_retries(body, list(questions))
        result = parse_response(payload, questions)
        self.credits_used += result.credits_used
        log.info(
            "jev ok model=%s questions=%s credits=%s answers=%s",
            self.model,
            list(questions),
            result.credits_used,
            {k: _brief(v) for k, v in result.answers.items()},
        )
        return result

    async def ask_batch(self, states: list[Any], questions: dict[str, dict[str, Any]]) -> list[JevResult]:
        """One request for many states (Laya `POST /v1/systemone/batch`, up to 64 states per call)."""
        if not states:
            return []
        body = build_request_body(self.model, None, questions)
        del body["state"]
        body = {**body, "states": states, **self._extra_body}
        async with self._semaphore:
            payload = await self._post_with_retries(body, list(questions), self._batch_url)
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != len(states):
            raise JevInvalidResponse("batch response does not match the number of states")
        return [parse_response(item, questions) for item in items]

    async def _post_with_retries(self, body: dict[str, Any], qids: list[str], url: str | None = None) -> Any:
        url = url or self._url
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                await self._sleep(self._backoff * 2 ** (attempt - 1))
            try:
                resp = await self._http.post(url, json=body, headers=self._headers)
            except httpx.ConnectError as exc:
                if self._fail_fast_connect:
                    raise JevUnreachable(f"no se puede conectar con {url}") from exc
                last = exc
                continue
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                log.warning("jev transport error (%s), attempt %d", type(exc).__name__, attempt + 1)
                continue
            self.requests_made += 1
            if resp.status_code == 401:
                raise JevAuthError("Jev API: clave invalida (401). Revisa JEV_API_KEY.")
            if resp.status_code == 402:
                raise JevCreditsError("Jev API: sin creditos (402).")
            if resp.status_code in RETRY_STATUS:
                last = JevTransientError(f"HTTP {resp.status_code}")
                log.warning("jev HTTP %s for %s, attempt %d", resp.status_code, qids, attempt + 1)
                continue
            if resp.status_code >= 400:
                raise JevRequestError(f"Jev API HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise JevInvalidResponse("response is not valid JSON") from exc
        raise JevTransientError(f"Jev API: reintentos agotados ({last})")


def _brief(answer: dict[str, Any]) -> Any:
    return answer.get("choice") or answer.get("score") or answer.get("noul")
