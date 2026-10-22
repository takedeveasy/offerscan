"""Rule-based fallback with the same interface and answer shape as JevClient."""

from __future__ import annotations

import re
from typing import Any

from offerscan.ai.jev_client import JevResult
from offerscan.ai.questions import EXPERIENCE_LEVELS, FIT_LEVELS, QUALITY_LEVELS
from offerscan.pipeline.normalize import strip_accents

MODEL_NAME = "heuristic-v1"

SENIOR_RE = re.compile(
    r"\b(senior|sr|lead|principal|head of|architect|arquitect[oa]s?|manager|staff|jef[ea]s?|responsables?|"
    r"directora?s?|lider(?:es)?|expert[oa]s?|tech lead|team lead)\b"
)
INTERN_RE = re.compile(r"\b(practicas|becario|beca|intern|internship|trainee|apprentice|dual)\b")
JUNIOR_RE = re.compile(r"\b(junior|jr|entry level|graduate|recien titulado|sin experiencia|nivel inicial)\b")
MENTOR_TERMS = ("mentor", "formacion", "onboarding", "plan de carrera", "training", "aprendizaje", "career")
RED_FLAG_TERMS = (
    "sin remuneracion", r"no remunerad\w*", "unpaid", "falso autonomo", "autonomos?", "comision(?:es)?",
    "captacion", "multinivel",
)  # fmt: skip
RED_FLAG_RE = re.compile(r"\b(" + "|".join(RED_FLAG_TERMS) + r")\b")
# Sales words only flag the role itself; in a description they usually describe the product ("plataforma de ventas").
SALES_TITLE_RE = re.compile(r"\b(ventas|comercial(?:es)?)\b")
# "Mentorizaras a perfiles junior" / "equipo a cargo de juniors" describe the team, not the vacancy.
# Only role-task verbs: "buscamos perfiles junior" or "mentoring para juniors" are real junior evidence.
JUNIOR_AS_OBJECT_RE = re.compile(
    r"\b(mentorizar(?:as)?|guiar(?:as)?|liderar(?:as)?|supervisar(?:as)?|coordinar(?:as)?|a cargo de)"
    r"\b[^.\n]{0,40}?\b(juniors?|jr)\b"
)
ROLE_TERMS: dict[str, tuple[str, ...]] = {
    "data_ml": ("data", "machine learning", "ml ", "etl", "bi ", "analytics", "pandas", "spark"),
    "devops_cloud": ("devops", "sre", "kubernetes", "terraform", "cloud", "aws", "azure"),
    "qa": ("qa", "tester", "testing", "quality assurance", "selenium", "cypress"),
    "mobile": ("android", "ios", "swift", "kotlin", "flutter", "react native", "mobile"),
    "fullstack": ("fullstack", "full stack", "full-stack"),
    "frontend": ("frontend", "front-end", "front end", "react", "angular", "vue", "css", "javascript"),
    "backend": ("backend", "back-end", "back end", "python", "java", "django", "spring", "node", "api"),
}  # fmt: skip
ROLE_OPTIONS = ["backend", "frontend", "fullstack", "data_ml", "devops_cloud", "qa", "mobile", "other"]


def _flat(text: str) -> str:
    return strip_accents(text).lower()


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _one_hot(options: list[str], pick: str, conf: float) -> dict[str, Any]:
    rest = (1 - conf) / max(len(options) - 1, 1)
    return {o: round(conf if o == pick else rest, 3) for o in options}


def _choice(options: list[str], pick: str, conf: float) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": pick,
        "probabilities": _one_hot(options, pick, conf),
        "confidence": conf,
    }


def _score(levels: list[str], index: float, conf: float) -> dict[str, Any]:
    pick = levels[round(_clamp(index, 0, len(levels) - 1))]
    return {
        "type": "score",
        "score": pick,
        "index": _clamp(index, 0, len(levels) - 1),
        "probabilities": _one_hot(levels, pick, conf),
        "confidence": conf,
    }


def _noul(value: float) -> dict[str, Any]:
    return {"type": "noul", "noul": round(_clamp(value), 3), "probabilities": {}, "confidence": None}


class _OfferView:
    def __init__(self, state: dict[str, Any]):
        self.title = _flat(str(state.get("title", "")))
        self.text = _flat(f"{state.get('title', '')} {state.get('description', '')}")
        self.years: int | None = state.get("experience_years_min")
        self.skills = [str(s).lower() for s in state.get("skills") or []]
        self.has_salary = str(state.get("salary", "not stated")) != "not stated"
        self.is_senior = bool(SENIOR_RE.search(self.title))
        text = JUNIOR_AS_OBJECT_RE.sub(" ", self.text)
        self.is_junior = bool(JUNIOR_RE.search(text)) or bool(INTERN_RE.search(text))
        self.mentor_hits = sum(t in self.text for t in MENTOR_TERMS)

    def seniority(self) -> tuple[str, float]:
        """`junior` needs positive evidence (junior wording or 0-1 years); it is never the default."""
        if self.is_senior:
            return "senior", 0.85
        if INTERN_RE.search(self.title):
            return "internship", 0.8
        if JUNIOR_RE.search(self.title):
            return "junior", 0.85
        if self.years is not None:
            if self.years >= 5:
                return "senior", 0.75
            if self.years >= 3:
                return "mid", 0.7
            if self.years == 2:
                return "mid", 0.5
            if self.years <= 1:
                return "junior", 0.6
        if self.is_junior:
            return "junior", 0.55
        return "unclear", 0.4

    def experience_index(self) -> tuple[float, float]:
        if self.years is None:
            return (0.0 if self.is_junior else 2.0), 0.35
        if self.years == 0:
            return 0.0, 0.7
        if self.years <= 2:
            return 2.0, 0.7
        return (3.0 if self.years <= 4 else 4.0), 0.75


JUNIOR_TARGET_OPTIONS = ["junior", "con_experiencia", "no_claro"]


def _junior_target(v: _OfferView) -> dict[str, Any]:
    """Same shape as Laya's `junior_target`, derived from the seniority rules."""
    pick, conf = v.seniority()
    if pick in ("internship", "junior"):
        return _choice(JUNIOR_TARGET_OPTIONS, "junior", conf)
    if pick in ("mid", "senior"):
        return _choice(JUNIOR_TARGET_OPTIONS, "con_experiencia", conf)
    return _choice(JUNIOR_TARGET_OPTIONS, "no_claro", conf)


def _answer_offer(qid: str, state: dict[str, Any]) -> dict[str, Any]:
    v = _OfferView(state)
    if qid == "seniority":
        pick, conf = v.seniority()
        return _choice(["internship", "junior", "mid", "senior", "unclear"], pick, conf)
    if qid == "junior_target":
        return _junior_target(v)
    if qid == "role_family":
        for role, terms in ROLE_TERMS.items():
            if any(re.search(rf"(?<!\w){re.escape(t.strip())}(?!\w)", v.text) for t in terms):
                return _choice(ROLE_OPTIONS, role, 0.6)
        return _choice(ROLE_OPTIONS, "other", 0.4)
    if qid == "experience_required":
        index, conf = v.experience_index()
        return _score(EXPERIENCE_LEVELS, index, conf)
    if qid == "junior_friendly":
        value = 0.5 + (0.3 if v.is_junior else 0) + (0.1 if v.mentor_hits else 0)
        value -= 0.5 if v.is_senior else 0
        if v.years is not None:
            value += {0: 0.2, 1: 0.05, 2: -0.15}.get(v.years, -0.4)
        return _noul(value)
    if qid == "mentorship":
        return _noul(0.1 + 0.25 * v.mentor_hits if v.mentor_hits else 0.1)
    if qid == "red_flags":
        hits = len({m.group(1) for m in RED_FLAG_RE.finditer(v.text)}) + bool(SALES_TITLE_RE.search(v.title))
        return _noul(0.05 + 0.25 * hits)
    if qid == "offer_quality":
        points = (
            (len(v.text) > 300) + (len(v.text) > 1000) + v.has_salary + (len(v.skills) >= 3)
        )
        return _score(QUALITY_LEVELS, max(0, points - 1), 0.5)
    raise KeyError(f"heuristic engine does not know question {qid!r}")


def _answer_match(qid: str, state: dict[str, Any]) -> dict[str, Any]:
    offer, cand = state["offer"], state["candidate"]
    text = _flat(f"{offer.get('title', '')} {offer.get('summary', '')} {' '.join(offer.get('skills') or [])}")
    stack = [_flat(s) for s in cand.get("stack", [])]
    hits = sum(bool(re.search(rf"(?<!\w){re.escape(s)}(?!\w)", text)) for s in stack)
    stack_match = _clamp(0.35 * hits + (0.1 if hits else 0.0))
    if qid == "stack_match":
        return _noul(stack_match)
    if qid == "junior_fit":
        index = 3 * stack_match
        locs = [_flat(loc) for loc in cand.get("locations", [])]
        place = _flat(str(offer.get("location", "")))
        mode = offer.get("work_mode")
        if mode == "remote" or any(loc in place for loc in locs):
            index += 0.5
        if mode in cand.get("work_mode_pref", []) or mode == "unknown":
            index += 0.5
        years = offer.get("experience_years_min")
        if years is not None and years * 12 > cand.get("experience_months", 0) + 12:
            index -= 1
        return _score(FIT_LEVELS, index, 0.5)
    raise KeyError(f"heuristic engine does not know question {qid!r}")


class HeuristicEngine:
    model = MODEL_NAME
    engine_name = "heuristic"
    credits_used = 0.0

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> JevResult:
        is_match = isinstance(state, dict) and "candidate" in state
        answer = _answer_match if is_match else _answer_offer
        wanted = list(questions)
        if not is_match and "junior_target" not in questions:
            wanted.append("junior_target")  # extra signal; engines that cannot answer it simply omit it
        return JevResult(answers={qid: answer(qid, state) for qid in wanted})

    async def aclose(self) -> None:
        return None
