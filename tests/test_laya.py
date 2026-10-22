import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from offerscan.ai.evaluate import evaluate_offers
from offerscan.ai.jev_client import JevClient, JevUnreachable, parse_response
from offerscan.ai.laya import LayaHybridEngine, LayaUnavailable, make_laya_engine
from offerscan.ai.questions import (
    LAYA_OFFER_QUESTIONS_V1,
    LAYA_OFFER_VERSION,
    OFFER_QUESTIONS_V1,
    OFFER_VERSION,
    build_offer_state,
)
from offerscan.cli import app
from offerscan.models import JobOffer, make_offer_id
from offerscan.ranking.offer_score import latest_offer_decision, score_offer
from offerscan.settings import ScoringConfig, Settings
from offerscan.storage.db import STATUS_CANDIDATE, Database

FIX = Path(__file__).parent / "fixtures" / "laya"
URL = "http://127.0.0.1:8000/v1/systemone"
BATCH_URL = URL + "/batch"



def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def offer(ext="1", title="Fullstack Developer Junior", desc="Python y React") -> JobOffer:
    from datetime import datetime, timezone

    return JobOffer(
        id=make_offer_id("t", ext), source="t", external_id=ext, url="u", title=title, company="c",
        scraped_at=datetime(2026, 10, 21, tzinfo=timezone.utc), description_md=desc,
    )  # fmt: skip


def test_bare_laya_response_parses_with_x_jev_confidence():
    res = parse_response(load("junior_offer"), LAYA_OFFER_QUESTIONS_V1)
    assert res.credits_used == 0
    sen = res.answers["seniority"]
    assert sen["choice"] == "junior"
    raw = load("junior_offer")["answers"]["seniority"]
    assert sen["confidence"] == raw["x_jev_confidence"] != raw["confidence"]
    exp = res.answers["experience_required"]
    assert exp["index"] == pytest.approx(1.6247) and exp["score"] == "1_2y"
    assert res.usage["input_tokens"] > 0


def test_confidence_falls_back_when_no_x_jev_confidence():
    payload = load("junior_offer")
    del payload["answers"]["seniority"]["x_jev_confidence"]
    res = parse_response(payload, LAYA_OFFER_QUESTIONS_V1)
    assert res.answers["seniority"]["confidence"] == payload["answers"]["seniority"]["confidence"]


def test_envelope_still_accepted():
    env = {"code": 0, "data": {"result": {"answers": load("junior_offer")["answers"]}, "creditsUsed": 2}}
    assert parse_response(env, LAYA_OFFER_QUESTIONS_V1).credits_used == 2


def test_score_with_legend_only():
    q = {"s": {"type": "score", "instructions": "x"}}
    payload = {"answers": {"s": {"type": "score", "score": 1.2, "legend": {"0": "low", "1": "mid", "2": "high"}}}}
    ans = parse_response(payload, q).answers["s"]
    assert ans["score"] == "mid" and ans["index"] == pytest.approx(1.2)


@respx.mock
async def test_no_auth_header_without_key_and_extra_body():
    route = respx.post(URL).respond(200, json=load("junior_offer"))
    c = JevClient("", "http://127.0.0.1:8000", "multilingual", extra_body={"max_len": 4096})
    await c.ask({"a": 1}, LAYA_OFFER_QUESTIONS_V1)
    await c.aclose()
    req = route.calls[0].request
    assert "authorization" not in req.headers
    assert json.loads(req.content)["max_len"] == 4096


@respx.mock
async def test_no_extra_body_by_default():
    route = respx.post("https://thejevai.com/v1/systemone").respond(200, json={"code": 0, "data": {
        "result": {"answers": {k: {"type": "noul", "noul": 0.5} for k in ["a"]}}}})  # fmt: skip
    c = JevClient("k")
    await c.ask("s", {"a": {"type": "noul", "instructions": "?"}})
    assert "max_len" not in json.loads(route.calls[0].request.content)


def engine():
    return make_laya_engine(Settings(laya_api_key=None))


@respx.mock
async def test_hybrid_merges_laya_and_heuristic():
    respx.post(URL).respond(200, json=load("junior_offer"))
    eng = engine()
    res = await eng.ask(build_offer_state(offer()), OFFER_QUESTIONS_V1)
    await eng.aclose()
    a = res.answers
    assert set(OFFER_QUESTIONS_V1) <= set(a) and "junior_target" in a
    assert a["seniority"]["confidence"] == pytest.approx(0.9279, abs=1e-3)  # from Laya
    assert a["junior_friendly"]["noul"] == pytest.approx(0.98, abs=0.01)  # P(junior), not Laya's noul
    assert a["role_family"]["type"] == "choice" and "mentorship" in a  # heuristic


@respx.mock
async def test_hybrid_match_questions_never_call_laya():
    route = respx.post(URL).respond(200, json=load("junior_offer"))
    from offerscan.ai.questions import MATCH_QUESTIONS_V1, build_match_state
    from offerscan.models import JuniorProfile

    state = build_match_state(offer(), JuniorProfile(slug="a", stack=["python"]))
    res = await engine().ask(state, MATCH_QUESTIONS_V1)
    assert "junior_fit" in res.answers and route.call_count == 0


@respx.mock
async def test_unreachable_server_message_in_spanish():
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    eng = engine()
    with pytest.raises(LayaUnavailable, match="scripts"):
        await eng.ask(build_offer_state(offer()), OFFER_QUESTIONS_V1)
    respx.get("http://127.0.0.1:8000/health").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(JevUnreachable, match="laya.ps1"):
        await eng.preflight()


@respx.mock
async def test_evaluate_with_laya_uses_distinct_version_and_ranks():
    respx.post(BATCH_URL).respond(200, json={"results": [load("junior_offer")]})
    db = Database(":memory:")
    o = offer()
    db.upsert_offers([o])
    db.set_status(o.id, STATUS_CANDIDATE)
    eng = engine()
    stats = await evaluate_offers(db, eng, [o])
    assert stats.evaluated == 1
    assert db.get_decision(o.id, LAYA_OFFER_VERSION, "laya:multilingual")
    assert db.get_decision(o.id, OFFER_VERSION, "laya:multilingual") is None
    decision = latest_offer_decision(db, o.id)
    assert score_offer(o, decision, ScoringConfig()).score > 50


@respx.mock
async def test_stage_stops_when_server_dies_mid_run():
    respx.post(BATCH_URL).mock(side_effect=httpx.ConnectError("refused"))
    db = Database(":memory:")
    o = offer()
    db.upsert_offers([o])
    stats = await evaluate_offers(db, engine(), [o])
    assert stats.evaluated == 0 and "laya.ps1" in stats.stopped_reason


@respx.mock
async def test_evaluate_uses_one_batch_call_per_64_offers():
    route = respx.post(BATCH_URL).mock(
        side_effect=lambda request: httpx.Response(
            200, json={"results": [load("junior_offer")] * len(json.loads(request.content)["states"])}
        )
    )
    single = respx.post(URL).respond(200, json=load("junior_offer"))
    db = Database(":memory:")
    offers = [offer(str(i)) for i in range(70)]
    db.upsert_offers(offers)
    seen = []
    stats = await evaluate_offers(db, engine(), offers, on_progress=seen.append)
    assert stats.evaluated == 70 and route.call_count == 2 and single.call_count == 0
    assert [len(json.loads(c.request.content)["states"]) for c in route.calls] == [64, 6]
    assert json.loads(route.calls[0].request.content)["max_len"] == 4096
    assert seen[-1] == 70
    again = await evaluate_offers(db, engine(), offers)
    assert again.cached == 70 and again.evaluated == 0 and route.call_count == 2


@respx.mock
async def test_bad_batch_falls_back_to_single_requests():
    respx.post(BATCH_URL).respond(200, json={"results": []})  # wrong length -> invalid response
    single = respx.post(URL).respond(200, json=load("junior_offer"))
    db = Database(":memory:")
    offers = [offer("a"), offer("b")]
    db.upsert_offers(offers)
    stats = await evaluate_offers(db, engine(), offers)
    assert stats.evaluated == 2 and single.call_count == 2


@respx.mock
async def test_ask_batch_matches_single_ask():
    respx.post(URL).respond(200, json=load("junior_offer"))
    respx.post(BATCH_URL).respond(200, json={"results": [load("junior_offer")]})
    eng = engine()
    state = build_offer_state(offer())
    one = await eng.ask(state, OFFER_QUESTIONS_V1)
    [batched] = await eng.ask_batch([state], OFFER_QUESTIONS_V1)
    await eng.aclose()
    assert batched.answers == one.answers


def test_cli_laya_down_exits_with_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LAYA_BASE_URL", "http://127.0.0.1:9")
    result = CliRunner().invoke(app, ["evaluate", "--engine", "laya"])
    assert result.exit_code == 1 and "laya.ps1" in result.output.replace("\n", "")


@pytest.mark.live
async def test_live_local_server():
    settings = Settings()
    eng = make_laya_engine(settings)
    try:
        await eng.preflight()
        res = await eng.ask(build_offer_state(offer(desc="Prácticas para estudiantes, sin experiencia")), OFFER_QUESTIONS_V1)
    finally:
        await eng.aclose()
    assert res.answers["junior_target"]["choice"] in {"junior", "con_experiencia", "no_claro"}


def test_engine_class_exported():
    assert LayaHybridEngine.engine_name == "laya"
