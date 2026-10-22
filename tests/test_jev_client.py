import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from offerscan.ai.evaluate import evaluate_offers, match_offers
from offerscan.ai.jev_client import (
    JevAuthError,
    JevBudgetExceeded,
    JevClient,
    JevCreditsError,
    JevInvalidResponse,
    JevRequestError,
    JevTransientError,
    build_request_body,
    parse_response,
)
from offerscan.ai.questions import OFFER_QUESTIONS_V1
from offerscan.models import JobOffer, JuniorProfile, make_offer_id
from offerscan.storage.db import STATUS_CANDIDATE, STATUS_EVALUATED, Database

URL = "https://thejevai.com/v1/systemone"
SECRET = "sk_test_secret_value"
Q = {
    "seniority": OFFER_QUESTIONS_V1["seniority"],
    "experience_required": OFFER_QUESTIONS_V1["experience_required"],
    "junior_friendly": OFFER_QUESTIONS_V1["junior_friendly"],
}


def envelope(answers, credits=1):
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "result": {"answers": answers, "usage": {"input_tokens": 42, "output_tokens": 0}, "elapsedMs": 180},
            "creditsUsed": credits,
        },
    }


GOOD = {
    "seniority": {"type": "choice", "choice": "junior", "probabilities": {"junior": 0.9}, "confidence": 0.8},
    "experience_required": {"type": "score", "score": "1_2y", "probabilities": {}, "confidence": 0.7},
    "junior_friendly": {"type": "noul", "noul": 0.94},
}


async def no_sleep(_):
    return None


def client(**kw):
    return JevClient(SECRET, model="typesafe/jev-1.13", sleep=no_sleep, **kw)


def test_build_request_body_shape():
    body = build_request_body("m", {"a": 1}, Q)
    assert set(body) == {"model", "state", "questions"}
    assert body["questions"]["seniority"]["criteria"]["junior"]
    assert body["questions"]["experience_required"]["criteria"][0] == "none"
    assert "criteria" not in body["questions"]["junior_friendly"]


def test_parse_response_normalizes_and_is_tolerant():
    answers = dict(GOOD)
    answers["experience_required"] = {"type": "score", "score": 2}  # numeric index, no confidence
    result = parse_response(envelope(answers, credits=3), Q)
    assert result.credits_used == 3 and result.elapsed_ms == 180
    assert result.answers["experience_required"]["score"] == "1_2y"
    assert result.answers["experience_required"]["confidence"] is None
    assert result.answers["junior_friendly"]["noul"] == 0.94


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 1, "message": "bad"},
        {"code": 0, "data": {}},
        {"code": 0, "data": {"result": {"answers": {}}}},
        {"code": 0, "data": {"result": {"answers": {"seniority": GOOD["seniority"]}}}},  # missing ids
        "nope",
    ],
)
def test_parse_response_invalid(payload):
    with pytest.raises(JevInvalidResponse):
        parse_response(payload, Q)


@respx.mock
async def test_success_sends_auth_and_body():
    route = respx.post(URL).respond(200, json=envelope(GOOD))
    c = client()
    result = await c.ask({"title": "x"}, Q)
    await c.aclose()
    req = route.calls[0].request
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    sent = json.loads(req.content)
    assert sent["model"] == "typesafe/jev-1.13" and sent["state"] == {"title": "x"}
    assert result.answers["seniority"]["choice"] == "junior"
    assert c.credits_used == 1
    assert SECRET not in repr(c)


@respx.mock
async def test_401_and_402_raise_without_retry():
    route = respx.post(URL).respond(401, json={"error": "bad key"})
    with pytest.raises(JevAuthError):
        await client().ask("s", Q)
    assert route.call_count == 1
    respx.post(URL).respond(402, json={})
    with pytest.raises(JevCreditsError):
        await client().ask("s", Q)


@respx.mock
async def test_429_then_success_retries():
    route = respx.post(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(529), httpx.Response(200, json=envelope(GOOD))]
    )
    result = await client().ask("s", Q)
    assert route.call_count == 3 and result.answers


@respx.mock
async def test_retries_are_bounded():
    route = respx.post(URL).respond(503)
    with pytest.raises(JevTransientError):
        await client(max_retries=2).ask("s", Q)
    assert route.call_count == 3


@respx.mock
async def test_timeout_retries():
    route = respx.post(URL).mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(200, json=envelope(GOOD))])
    await client().ask("s", Q)
    assert route.call_count == 2


@respx.mock
async def test_400_and_code_nonzero():
    respx.post(URL).respond(422, json={"error": "invalid question"})
    with pytest.raises(JevRequestError):
        await client().ask("s", Q)
    respx.post(URL).respond(200, json={"code": 500, "message": "boom"})
    with pytest.raises(JevInvalidResponse):
        await client().ask("s", Q)


@respx.mock
async def test_budget_stops_requests():
    route = respx.post(URL).respond(200, json=envelope(GOOD, credits=2))
    c = client(max_credits=2)
    await c.ask("s", Q)
    with pytest.raises(JevBudgetExceeded):
        await c.ask("s", Q)
    assert route.call_count == 1


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def offer(ext: str) -> JobOffer:
    return JobOffer(
        id=make_offer_id("t", ext), source="t", external_id=ext, url="u", title="Junior dev",
        company="c", scraped_at=NOW, description_md="Python junior",
    )  # fmt: skip


@respx.mock
async def test_evaluate_caches_and_marks_evaluated():
    all_answers = {
        qid: {"type": q["type"], **({"choice": "junior"} if q["type"] == "choice" else {}),
              **({"score": q["criteria"][0]} if q["type"] == "score" else {}),
              **({"noul": 0.5} if q["type"] == "noul" else {})}
        for qid, q in OFFER_QUESTIONS_V1.items()
    }  # fmt: skip
    route = respx.post(URL).respond(200, json=envelope(all_answers))
    db = Database(":memory:")
    offers = [offer("1"), offer("2")]
    db.upsert_offers(offers)
    for o in offers:
        db.set_status(o.id, STATUS_CANDIDATE)
    c = client()
    stats = await evaluate_offers(db, c, offers)
    assert stats.evaluated == 2 and route.call_count == 2
    again = await evaluate_offers(db, c, offers)
    assert again.cached == 2 and route.call_count == 2
    assert len(db.offers_by_status(STATUS_EVALUATED)) == 2


@respx.mock
async def test_evaluate_stops_on_no_credits_and_leaves_offers_pending():
    respx.post(URL).respond(402, json={})
    db = Database(":memory:")
    offers = [offer("1"), offer("2")]
    db.upsert_offers(offers)
    for o in offers:
        db.set_status(o.id, STATUS_CANDIDATE)
    stats = await evaluate_offers(db, client(concurrency=1), offers)
    assert stats.evaluated == 0 and "402" in stats.stopped_reason
    assert len(db.offers_by_status(STATUS_CANDIDATE)) == 2
    assert not db.all_decisions()


@respx.mock
async def test_match_questions_use_junior_without_pii():
    route = respx.post(URL).respond(
        200,
        json=envelope(
            {"junior_fit": {"type": "score", "score": "good"}, "stack_match": {"type": "noul", "noul": 0.8}}
        ),
    )
    junior = JuniorProfile(slug="ana", stack=["python"])
    db = Database(":memory:")
    decisions, stats = await match_offers(db, client(), junior, [offer("1")])
    assert stats.evaluated == 1 and len(decisions) == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["state"]["candidate"]["stack"] == ["python"]
    assert "slug" not in json.dumps(sent["state"])
