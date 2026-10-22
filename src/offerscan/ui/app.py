"""Local web UI (FastAPI + Jinja2 + HTMX). All pipeline work goes through `offerscan.service`."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from offerscan import service
from offerscan.geo import PROVINCES, REMOTE
from offerscan.models import JuniorProfile, WorkMode
from offerscan.pipeline.normalize import strip_accents
from offerscan.report.render import _salary
from offerscan.service import ResultFilters, SearchFilters
from offerscan.settings import (
    ScoringConfig,
    Settings,
    load_juniors,
    load_scoring_config,
    load_search_config,
)
from offerscan.storage.db import LABEL_JUNIOR, LABEL_NOT_JUNIOR, Database
from offerscan.ui.jobs import RunManager, Runner

HERE = Path(__file__).parent
PAGE_SIZE = 100
PORTAL_GROUPS = {
    "Portales de empleo en España": {
        "tecnoempleo": "Tecnoempleo",
        "infojobs": "InfoJobs",
        "infoempleo": "Infoempleo",
        "trabajos": "Trabajos.com",
        "jobfluent": "JobFluent",
        "manfred": "Manfred",
        "linkedin": "LinkedIn",
    },
    "Agregadores (reúnen ofertas de muchas webs)": {
        "indeed": "Indeed",
        "glassdoor": "Glassdoor",
        "talent": "Talent.com",
        "jooble": "Jooble",
    },
    "Remoto internacional (en inglés)": {
        "himalayas": "Himalayas",
        "remotive": "Remotive",
    },
}
PORTALS = {key: label for group in PORTAL_GROUPS.values() for key, label in group.items()}
MODES = {"onsite": "Presencial", "hybrid": "Híbrido", "remote": "Remoto", "unknown": "Sin especificar"}
BUCKETS = {"top": "Mejores ofertas", "review": "Para revisar", "discard": "Descartadas"}
LABEL_TEXT = {LABEL_JUNIOR: "Sí es junior", LABEL_NOT_JUNIOR: "No es junior"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FILTER_NOTES = {
    "tecnoempleo": "Provincia, modalidad y experiencia se filtran en el propio portal.",
    "infojobs": "Provincia y categoría «Informática y telecomunicaciones» se filtran en el portal; "
    "la modalidad se filtra después de rastrear.",
    "linkedin": "Solo admite una ubicación en el portal; provincia y modalidad se filtran después de rastrear.",
    "infoempleo": "Provincia en el portal. Con «0 años» de experiencia busca en su sección «Primer empleo».",
    "trabajos": "Provincia en el portal; una página por tecnología y provincia, con detalle de cada oferta.",
    "jobfluent": "Startups. Solo tiene Madrid, Barcelona, Valencia y remoto; otras provincias no dan resultados.",
    "manfred": "Pocas ofertas pero todas tech y con salario. Se descargan todas y se filtran por tu tecnología.",
    "indeed": "Provincia y remoto en el portal. Solo la primera página (15 ofertas): la segunda pide iniciar sesión.",
    "glassdoor": "Búsqueda en toda España (una página, ~30 ofertas); provincia y modalidad se filtran después.",
    "talent": "Agregador. Provincia en el portal; muchas ofertas repetidas de otras webs (se deduplican).",
    "jooble": "Agregador. Provincia en el portal; trae la descripción completa sin abrir cada oferta.",
    "himalayas": "Solo remoto, abierto a residentes en España y de nivel inicial. Ofertas en inglés.",
    "remotive": "Solo remoto (mundial o Europa), en inglés. Úsalo de vez en cuando: pide pocas consultas al día.",
}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


def _csv_list(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n;]", text or "") if x.strip()]


def make_default_runner(settings: Settings, db: Database) -> Runner:
    def run(filters: SearchFilters, hooks: service.SearchHooks) -> service.SearchSummary:
        return service.run_search(
            db,
            settings,
            load_search_config(settings.config_dir),
            load_scoring_config(settings.config_dir),
            load_juniors(settings.config_dir),
            filters,
            hooks,
        )

    return run


def create_app(
    settings: Settings | None = None,
    runner: Runner | None = None,
    port: int = 8765,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.db_path)
    manager = RunManager(runner or make_default_runner(settings, db))
    hosts = allowed_hosts or LOCAL_HOSTS
    app = FastAPI(title="offerscan", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.manager, app.state.settings = db, manager, settings
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["portal_name"] = lambda name: PORTALS.get(name, name)
    templates.env.globals.update(
        PROVINCES=[p.name for p in PROVINCES],
        REMOTE=REMOTE,
        TECHS=service.TECH_SUGGESTIONS,
        PORTALS=PORTALS,
        PORTAL_GROUPS=PORTAL_GROUPS,
        MODES=MODES,
        BUCKETS=BUCKETS,
        FILTER_NOTES=FILTER_NOTES,
        format_salary=lambda o: _salary(o.salary_min, o.salary_max),
        reason_es=service.reason_es,
    )

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Defence against DNS rebinding / cross-site posts: only talk to our own origin."""
        host = urlparse("//" + request.headers.get("host", "")).hostname
        origin = request.headers.get("origin")
        origin_host = urlparse(origin).hostname if origin else None
        if host not in hosts or (origin_host is not None and origin_host not in hosts):
            return JSONResponse({"error": "host no permitido"}, status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------------ helpers
    def scoring_for(junior_salary_max: int | None) -> ScoringConfig:
        scoring = load_scoring_config(settings.config_dir)
        return scoring.model_copy(update={"junior_salary_max": junior_salary_max}) if junior_salary_max else scoring

    def base_search():
        return load_search_config(settings.config_dir)

    def searches_dir() -> Path:
        return settings.config_dir / "searches"

    def saved_searches() -> dict[str, SearchFilters]:
        out = {}
        for path in sorted(searches_dir().glob("*.yaml")):
            try:
                out[path.stem] = SearchFilters.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
            except (ValidationError, yaml.YAMLError):
                continue
        return out

    def save_search(filters: SearchFilters) -> str:
        parts = [*filters.keywords[:2], *[strip_accents(x) for x in filters.locations[:2]]]
        slug = _slugify("-".join(parts)) or "busqueda"
        searches_dir().mkdir(parents=True, exist_ok=True)
        (searches_dir() / f"{slug}.yaml").write_text(
            yaml.safe_dump(filters.model_dump(mode="json"), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return slug

    def default_filters() -> SearchFilters:
        cfg = base_search()
        sources = [s for s in cfg.enabled_sources() if s != "linkedin"] or ["tecnoempleo"]
        return SearchFilters(
            keywords=cfg.keywords[:3],
            locations=list(cfg.locations),
            sources=sources,
            max_experience_years=cfg.prefilter.max_experience_years,
            max_age_days=cfg.prefilter.max_age_days,
            junior_salary_max=load_scoring_config(settings.config_dir).junior_salary_max,
            max_pages=1,
        )

    # ------------------------------------------------------------------ search
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/buscar")

    @app.get("/buscar", response_class=HTMLResponse)
    def search_form(request: Request, guardada: str = ""):
        state = manager.snapshot()
        filters = saved_searches().get(guardada) or state.filters or default_filters()
        known = {t.lower() for t in service.TECH_SUGGESTIONS}
        return page(
            request,
            "search.html",
            active="buscar",
            f=filters,
            tech_extra=", ".join(k for k in filters.keywords if k.lower() not in known),
            saved=list(saved_searches()),
            state=state,
            laya_url=settings.laya_base_url,
        )

    @app.post("/buscar", response_class=HTMLResponse)
    async def search_start(request: Request):
        form = await request.form()
        techs = [t for t in form.getlist("techs") if t]
        keywords = list(dict.fromkeys([*techs, *_csv_list(str(form.get("tech_extra", "")))]))
        filters_data = dict(
            keywords=keywords,
            locations=[x for x in form.getlist("provinces") if x],
            sources=[x for x in form.getlist("sources") if x in PORTALS],
            work_modes=[x for x in form.getlist("work_modes") if x in MODES],
            max_experience_years=_int(str(form.get("max_experience", ""))) or 0,
            max_age_days=_int(str(form.get("max_age_days", ""))) or 30,
            salary_min=_int(str(form.get("salary_min", ""))),
            junior_salary_max=_int(str(form.get("junior_salary_max", ""))) or 35_000,
            engine=str(form.get("engine", "heuristic")),
            max_pages=min(max(_int(str(form.get("max_pages", ""))) or 1, 1), 5),
            match_juniors=form.get("match_juniors") is not None,
        )
        problems = []
        if not keywords:
            problems.append("Elige al menos una tecnología o escribe una palabra clave.")
        if not filters_data["sources"]:
            problems.append("Elige al menos un portal.")
        if filters_data["engine"] not in ("heuristic", "laya", "jev"):
            filters_data["engine"] = "heuristic"
        if filters_data["engine"] == "laya" and not service.laya_available(settings.laya_base_url):
            problems.append(
                f"El motor Laya no responde en {settings.laya_base_url}. Arráncalo con .\\scripts\\laya.ps1 "
                "(Windows) o bash scripts/laya.sh (Mac/Linux), o elige el motor heurístico."
            )
        if problems:
            return page(request, "_progreso.html", 422, state=manager.snapshot(), problems=problems)
        filters = SearchFilters(**filters_data)
        if not manager.start(filters):
            return page(request, "_progreso.html", 409, state=manager.snapshot(), busy=True)
        save_search(filters)
        return page(request, "_progreso.html", state=manager.snapshot())

    @app.get("/progreso", response_class=HTMLResponse)
    def progress(request: Request):
        state = manager.snapshot()
        # Only the poll that sees the run finish sends the user on; reopening Búsqueda later must not bounce them.
        auto_redirect = bool(request.headers.get("HX-Request")) and state.status == "done"
        return page(request, "_progreso.html", state=state, auto_redirect=auto_redirect)

    @app.get("/progreso/resultados")
    def progress_results(request: Request):
        """Results of the last run, on the first tab that has offers (never an empty «Mejores ofertas»)."""
        filters = manager.snapshot().filters
        params: dict[str, str] = {"bucket": "top"}
        if filters is not None:
            params["junior_salary_max"] = str(filters.junior_salary_max)
            if filters.salary_min:
                params["salary_min"] = str(filters.salary_min)
        counts = results_context(QueryParams(params))["counts"]
        if not counts.get("top") and counts.get("review"):
            params["bucket"] = "review"
        url = "/resultados?" + urlencode(params)
        if request.headers.get("HX-Request"):
            return Response(headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    @app.get("/laya", response_class=HTMLResponse)
    def laya_status(request: Request, engine: str = "heuristic"):
        if engine != "laya":
            return HTMLResponse("")
        up = service.laya_available(settings.laya_base_url)
        return page(request, "_laya.html", up=up, url=settings.laya_base_url)

    # ------------------------------------------------------------------ results
    def parse_filters(params: QueryParams) -> tuple[ResultFilters, int, int | None]:
        bucket = params.get("bucket", "top")
        f = ResultFilters(
            q=params.get("q", "").strip(),
            techs=[x for x in params.getlist("techs") if x],
            provinces=[x for x in params.getlist("provinces") if x],
            sources=[x for x in params.getlist("sources") if x],
            work_mode=params.get("work_mode", ""),
            bucket=bucket if bucket in (*BUCKETS, "all") else "top",
            salary_min=_int(params.get("salary_min")),
            label=params.get("label", ""),
            junior=params.get("junior", ""),
        )
        return f, max(_int(params.get("pagina")) or 1, 1), _int(params.get("junior_salary_max"))

    def query_string(f: ResultFilters, junior_salary_max: int | None, **override) -> str:
        data = f.model_dump() | override
        pairs: list[tuple[str, str]] = []
        for key, value in data.items():
            if isinstance(value, list):
                pairs += [(key, v) for v in value]
            elif value not in (None, ""):
                pairs.append((key, str(value)))
        if junior_salary_max:
            pairs.append(("junior_salary_max", str(junior_salary_max)))
        return urlencode(pairs)

    def results_context(params: QueryParams) -> dict:
        f, pagina, junior_salary_max = parse_filters(params)
        scoring = scoring_for(junior_salary_max)
        views = service.list_offer_views(db, scoring)
        counts = service.bucket_counts(views, f)
        selected = service.filter_offer_views(views, f)
        pages = max((len(selected) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        pagina = min(pagina, pages)
        return dict(
            f=f,
            junior_salary_max=junior_salary_max or scoring.junior_salary_max,
            counts=counts,
            total=len(selected),
            views=selected[(pagina - 1) * PAGE_SIZE : pagina * PAGE_SIZE],
            pagina=pagina,
            pages=pages,
            qs=lambda **kw: query_string(f, junior_salary_max, **kw),
            juniors=[j.slug for j in load_juniors(settings.config_dir)],
            has_data=bool(db.offer_rows()),
            now=datetime.now(timezone.utc),
        )

    @app.get("/resultados", response_class=HTMLResponse)
    def results(request: Request):
        ctx = results_context(request.query_params)
        partial = request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request")
        return page(request, "_resultados.html" if partial else "results.html", active="resultados", **ctx)

    @app.get("/exportar.csv")
    def export_csv(request: Request):
        f, _pagina, junior_salary_max = parse_filters(request.query_params)
        views = service.filter_offer_views(service.list_offer_views(db, scoring_for(junior_salary_max)), f)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["puntuacion", "categoria", "titulo", "empresa", "ubicacion", "modalidad", "salario",
                         "portal", "fecha", "tecnologias", "motivos", "etiqueta", "url"])  # fmt: skip
        for v in views:
            o = v.offer
            writer.writerow([
                v.score if v.score is not None else "", BUCKETS[v.bucket], o.title, o.company, o.location,
                MODES[o.work_mode.value], _salary(o.salary_min, o.salary_max), o.source,
                o.published_at.date().isoformat() if o.published_at else "", ", ".join(v.techs),
                " | ".join(v.notes), LABEL_TEXT.get(v.label or "", ""),
                o.url,
            ])  # fmt: skip
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        return Response(
            "\ufeff" + out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="offerscan-{stamp}.csv"'},
        )

    @app.post("/etiqueta/{offer_id}/{label}", response_class=HTMLResponse)
    async def set_label(request: Request, offer_id: str, label: str):
        if db.get_offer(offer_id) is None:
            return HTMLResponse("Oferta no encontrada", status_code=404)
        if label not in (LABEL_JUNIOR, LABEL_NOT_JUNIOR, "ninguna"):
            return HTMLResponse("Etiqueta no válida", status_code=422)
        db.set_label(offer_id, None if label == "ninguna" else label)
        form = await request.form()
        scoring = scoring_for(_int(str(form.get("junior_salary_max", ""))))
        view = next((v for v in service.list_offer_views(db, scoring) if v.offer.id == offer_id), None)
        if view is None:
            return HTMLResponse("")
        return page(request, "_fila.html", v=view, now=datetime.now(timezone.utc))

    # ------------------------------------------------------------------ juniors
    def junior_from_form(form) -> JuniorProfile:
        return JuniorProfile(
            slug=str(form.get("slug", "")).strip(),
            stack=[*form.getlist("techs"), *_csv_list(str(form.get("tech_extra", "")))],
            nice_to_have=_csv_list(str(form.get("nice_to_have", ""))),
            languages=_csv_list(str(form.get("languages", ""))),
            locations=[x for x in form.getlist("provinces") if x],
            work_mode_pref=[WorkMode(x) for x in form.getlist("work_modes") if x in MODES and x != "unknown"],
            min_salary=_int(str(form.get("min_salary", ""))),
            experience_months=_int(str(form.get("experience_months", ""))) or 0,
            interests=_csv_list(str(form.get("interests", ""))),
        )

    def junior_page(request: Request, junior: JuniorProfile | None, status: int = 200, errors: list[str] | None = None):
        known = {t.lower() for t in service.TECH_SUGGESTIONS}
        extra = ", ".join(s for s in (junior.stack if junior else []) if s.lower() not in known)
        rows = []
        if junior is not None:
            offers = {v.offer.id: v for v in service.list_offer_views(db, scoring_for(None))}
            for m in sorted(db.matches_for(junior.slug), key=lambda m: m.final_score, reverse=True):
                if m.offer_id in offers:
                    rows.append((m, offers[m.offer_id]))
        return page(request, "junior_form.html", status, active="juniors", j=junior, tech_extra=extra,
                    rows=rows, errors=errors or [], is_new=junior is None or not (settings.config_dir / "juniors"
                    / f"{junior.slug}.yaml").exists())  # fmt: skip

    @app.get("/juniors", response_class=HTMLResponse)
    def juniors_list(request: Request):
        juniors = load_juniors(settings.config_dir)
        counts = {j.slug: len(db.matches_for(j.slug)) for j in juniors}
        return page(request, "juniors.html", active="juniors", juniors=juniors, counts=counts)

    @app.get("/juniors/nuevo", response_class=HTMLResponse)
    def junior_new(request: Request):
        return junior_page(request, None)

    @app.get("/juniors/{slug}", response_class=HTMLResponse)
    def junior_edit(request: Request, slug: str):
        junior = next((j for j in load_juniors(settings.config_dir) if j.slug == slug), None)
        if junior is None:
            return RedirectResponse("/juniors")
        return junior_page(request, junior)

    @app.post("/juniors", response_class=HTMLResponse)
    async def junior_save(request: Request):
        form = await request.form()
        try:
            junior = junior_from_form(form)
        except ValidationError:
            return junior_page(request, None, 422, ["Revisa los datos del perfil."])
        errors = []
        if not SLUG_RE.match(junior.slug):
            errors.append("El identificador debe tener 2-41 caracteres: minúsculas, números y guiones.")
        if not junior.stack:
            errors.append("Añade al menos una tecnología al stack.")
        if errors:
            return junior_page(request, junior, 422, errors)
        service.save_junior(settings.config_dir, junior)
        return RedirectResponse(f"/juniors/{junior.slug}?guardado=1", status_code=303)

    @app.post("/juniors/{slug}/emparejar")
    def junior_match(slug: str):
        junior = next((j for j in load_juniors(settings.config_dir) if j.slug == slug), None)
        if junior is not None:
            service.match_single_junior(db, settings, scoring_for(None), junior)
        return RedirectResponse(f"/juniors/{slug}", status_code=303)

    return app
