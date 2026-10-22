import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient  # noqa: E402

from offerscan.demo import load_fixture_raws  # noqa: E402
from offerscan.scrapers.runner import SourceResult, post_filter  # noqa: E402
from offerscan.service import SearchFilters, SearchHooks, SearchSummary, evaluate_stage  # noqa: E402
from offerscan.settings import Settings, load_search_config  # noqa: E402
from offerscan.ui.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8765"


@pytest.fixture
def ctx(tmp_path):
    import shutil

    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    settings = Settings(config_dir=config, data_dir=tmp_path / "data", reports_dir=tmp_path / "reports")
    calls: list[SearchFilters] = []
    release = {"go": True}

    def fake_runner(filters: SearchFilters, hooks: SearchHooks) -> SearchSummary:
        """Simulated crawl: ingests the saved fixture pages instead of hitting the network."""
        from offerscan.pipeline.stages import ingest_raw

        calls.append(filters)
        cfg = load_search_config(settings.config_dir)
        hooks.stage("Rastreando")
        raws = load_fixture_raws(ROOT / "tests" / "fixtures")
        results = []
        for name in filters.sources:
            hooks.source_started(name)
            mine = [r for r in raws if r["source"] == name]
            for i in range(len(mine)):
                hooks.item(name, i + 1)
            kept = post_filter(name, mine, cfg)
            ingest_raw(app.state.db, kept, cfg)
            result = SourceResult(name, parsed=len(mine), stored=len(kept), filtered=len(mine) - len(kept))
            hooks.source_done(result)
            results.append(result)
        while not release["go"]:
            time.sleep(0.01)
        hooks.stage("Evaluando")
        prep, stats = evaluate_stage(app.state.db, settings, cfg, "heuristic", on_progress=hooks.evaluated)
        return SearchSummary(crawl=results, prepared=prep, evaluation=stats, engine="heuristic")

    app = create_app(settings, runner=fake_runner)
    client = TestClient(app, base_url=BASE)
    return client, app, calls, release


def wait_done(app):
    app.state.manager.join(20)
    assert app.state.manager.snapshot().status == "done", app.state.manager.snapshot().error


FORM = {"techs": ["python", "react"], "provinces": ["Madrid"], "sources": ["tecnoempleo"], "max_pages": "1"}


def test_pages_render_in_spanish(ctx):
    client, *_ = ctx
    for path, text in [("/buscar", "Buscar ofertas junior"), ("/resultados", "Resultados"), ("/juniors", "Perfiles junior")]:
        r = client.get(path)
        assert r.status_code == 200 and text in r.text
    home = client.get("/buscar").text
    assert "htmx.min.js" in home and "unpkg" not in home
    assert "fonts.googleapis" not in home and "fonts.gstatic" not in home
    assert "takedeveasy" in home and 'class="dev"' in home
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/logo-mark.svg").status_code == 200
    assert client.get("/static/fonts/space-grotesk-latin.woff2").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/buscar"


def test_search_validation_and_start(ctx):
    client, app, calls, _ = ctx
    bad = client.post("/buscar", data={"sources": ["tecnoempleo"]})
    assert bad.status_code == 422 and "al menos una tecnología" in bad.text
    ok = client.post("/buscar", data=FORM)
    assert ok.status_code == 200 and "Progreso" in ok.text
    wait_done(app)
    assert calls[0].keywords == ["python", "react"] and calls[0].locations == ["Madrid"]
    done = client.get("/progreso").text
    assert "Búsqueda terminada" in done and "hx-trigger" not in done and 'href="/progreso/resultados"' in done
    # the search was saved and can be reloaded
    assert (Path(app.state.settings.config_dir) / "searches" / "python-react-madrid.yaml").exists()
    assert "python-react-madrid" in client.get("/buscar").text
    assert "checked" in client.get("/buscar?guardada=python-react-madrid").text


def test_only_one_run_at_a_time(ctx):
    client, app, calls, release = ctx
    release["go"] = False
    assert client.post("/buscar", data=FORM).status_code == 200
    busy = client.post("/buscar", data=FORM)
    assert busy.status_code == 409 and "Ya hay una búsqueda en marcha" in busy.text
    running = client.get("/progreso").text
    assert 'hx-trigger="every 1s"' in running
    release["go"] = True
    wait_done(app)
    assert len(calls) == 1
    assert client.post("/buscar", data=FORM).status_code == 200  # free again
    wait_done(app)


def test_runner_failure_is_reported(tmp_path, monkeypatch):
    from offerscan.ai.laya import LayaUnavailable

    monkeypatch.setattr("offerscan.service.laya_available", lambda url: True)  # up at start, gone mid-run
    settings = Settings(config_dir=tmp_path / "config", data_dir=tmp_path / "d")

    def boom(filters, hooks):
        raise LayaUnavailable("Laya no responde")

    app = create_app(settings, runner=boom)
    client = TestClient(app, base_url=BASE)
    client.post("/buscar", data={**FORM, "engine": "laya"})
    app.state.manager.join(5)
    html = client.get("/progreso", headers={"HX-Request": "true"}).text
    assert "La búsqueda se detuvo" in html and "Laya no responde" in html
    assert "Te llevo a los resultados" not in html


def portal_app(tmp_path, outcomes: dict[str, str]):
    """App whose fake run finishes with each portal either "ok" or failing with the given error."""
    settings = Settings(config_dir=tmp_path / "config", data_dir=tmp_path / "d")

    def runner(filters, hooks):
        results = []
        for name, error in outcomes.items():
            hooks.source_started(name)
            result = SourceResult(name, error=error) if error != "ok" else SourceResult(name, parsed=5, stored=5)
            hooks.source_done(result)
            results.append(result)
        return SearchSummary(crawl=results, engine="heuristic")

    app = create_app(settings, runner=runner)
    client = TestClient(app, base_url=BASE)
    client.post("/buscar", data={**FORM, "sources": list(outcomes)})
    wait_done(app)
    return client


def test_finished_run_redirects_only_the_page_that_watched_it(tmp_path):
    client = portal_app(tmp_path, {"tecnoempleo": "ok"})
    polled = client.get("/progreso", headers={"HX-Request": "true"}).text
    assert "Búsqueda terminada" in polled and "Te llevo a los resultados" in polled
    assert 'hx-get="/progreso/resultados" hx-trigger="load delay:4s"' in polled
    # coming back to Búsqueda later shows the summary but must not bounce the user to Resultados
    assert "Te llevo a los resultados" not in client.get("/buscar").text


def test_results_link_lands_on_a_tab_with_offers(tmp_path, monkeypatch):
    client = portal_app(tmp_path, {"tecnoempleo": "ok"})
    plain = client.get("/progreso/resultados", follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"].startswith("/resultados?bucket=top")
    monkeypatch.setattr("offerscan.service.bucket_counts", lambda views, f: {"top": 0, "review": 3, "discard": 1})
    htmx = client.get("/progreso/resultados", headers={"HX-Request": "true"})
    assert htmx.headers["HX-Redirect"].startswith("/resultados?bucket=review")
    assert "junior_salary_max=35000" in htmx.headers["HX-Redirect"]


def test_every_portal_failing_is_an_error_not_a_success(tmp_path):
    client = portal_app(tmp_path, {"tecnoempleo": "captcha", "infojobs": "bloqueado"})
    html = client.get("/progreso", headers={"HX-Request": "true"}).text
    assert "Ningún portal ha devuelto ofertas" in html
    assert "Búsqueda terminada" not in html and "Te llevo a los resultados" not in html


def test_partial_portal_failure_is_called_out(tmp_path):
    client = portal_app(tmp_path, {"tecnoempleo": "ok", "infojobs": "bloqueado"})
    html = client.get("/progreso", headers={"HX-Request": "true"}).text
    assert "Búsqueda terminada" in html and "1 portal no ha respondido (InfoJobs)" in html


def test_lost_connection_is_announced_while_running(ctx):
    client, app, _, release = ctx
    release["go"] = False
    client.post("/buscar", data=FORM)
    running = client.get("/progreso").text
    assert "hx-on::send-error" in running and "Se ha perdido la conexión" in running
    release["go"] = True
    wait_done(app)
    assert "Se ha perdido la conexión" not in client.get("/progreso").text


def test_results_filters_labels_and_export(ctx):
    client, app, *_ = ctx
    client.post("/buscar", data={**FORM, "sources": ["tecnoempleo", "infojobs", "linkedin"], "provinces": []})
    wait_done(app)

    everything = client.get("/resultados?bucket=all").text
    assert "Mejores ofertas" in everything and "Para revisar" in everything and "Descartadas" in everything
    total = everything.count('<tr id="o-')
    assert total > 10

    only_te = client.get("/resultados?bucket=all&sources=tecnoempleo").text
    assert 0 < only_te.count('<tr id="o-') < total and 'badge src">LinkedIn' not in only_te

    text = client.get("/resultados?bucket=all&q=python", headers={"HX-Request": "true"}).text
    assert "<html" not in text and text.count('<tr id="o-') >= 1  # partial for htmx
    assert client.get("/resultados?bucket=all&q=zzzzzz-no-existe").text.count('<tr id="o-') == 0
    assert "No hay ofertas con estos filtros" in client.get("/resultados?bucket=all&q=zzzzzz-no-existe").text

    pythons = client.get("/resultados?bucket=all&techs=python").text.count('<tr id="o-')
    assert 0 < pythons < total
    remote = client.get("/resultados?bucket=all&work_mode=remote").text
    assert "Presencial</span>" not in remote.split("<tbody>")[1]

    # labels persist and feed the eval export
    offer_id = client.get("/resultados?bucket=all").text.split('<tr id="o-')[1].split('"')[0]
    row = client.post(f"/etiqueta/{offer_id}/no_junior", data={"junior_salary_max": "35000"})
    assert row.status_code == 200 and "No es junior</span>" in row.text
    assert app.state.db.labels() == {offer_id: "no_junior"}
    assert 'o-' + offer_id in client.get("/resultados?bucket=all&label=no_junior").text
    assert 'o-' + offer_id not in client.get("/resultados?bucket=all&label=none").text
    client.post(f"/etiqueta/{offer_id}/ninguna")
    assert app.state.db.labels() == {}
    assert client.post("/etiqueta/inexistente/junior").status_code == 404

    csv = client.get("/exportar.csv?bucket=all&sources=tecnoempleo")
    assert csv.status_code == 200 and csv.text.startswith("\ufeffpuntuacion,categoria,titulo")
    assert "text/csv" in csv.headers["content-type"] and len(csv.text.splitlines()) > 5


def test_labels_export_to_eval_jsonl(ctx, tmp_path):
    import json

    from offerscan.evaluation import export_labeled_offers, labeled_rows_from_db

    client, app, *_ = ctx
    client.post("/buscar", data={**FORM, "provinces": []})
    wait_done(app)
    ids = [o.id for o in app.state.db.all_offers()[:2]]
    app.state.db.set_label(ids[0], "junior")
    app.state.db.set_label(ids[1], "no_junior")
    rows = labeled_rows_from_db(app.state.db)
    assert {r["label"] for r in rows} == {True, False}
    assert set(rows[0]) == {"origin", "title", "company", "description", "label"}
    target = tmp_path / "labeled.jsonl"
    assert export_labeled_offers(app.state.db, target) == (2, 2)
    assert export_labeled_offers(app.state.db, target) == (0, 2)  # no duplicates
    assert len(json.loads(target.read_text(encoding="utf-8").splitlines()[0])) == 5


def test_junior_crud_and_matching(ctx):
    client, app, *_ = ctx
    client.post("/buscar", data={**FORM, "provinces": []})
    wait_done(app)
    bad = client.post("/juniors", data={"slug": "X", "techs": ["python"]})
    assert bad.status_code == 422 and "identificador" in bad.text
    ok = client.post(
        "/juniors",
        data={"slug": "ana-python", "techs": ["python"], "tech_extra": "django", "provinces": ["Madrid"],
              "work_modes": ["remote", "hybrid"], "languages": "es", "min_salary": "20000"},
        follow_redirects=False,
    )  # fmt: skip
    assert ok.status_code == 303
    yaml_path = Path(app.state.settings.config_dir) / "juniors" / "ana-python.yaml"
    assert "django" in yaml_path.read_text(encoding="utf-8")
    assert "ana-python" in client.get("/juniors").text
    assert "Ofertas emparejadas" in client.get("/juniors/ana-python").text
    assert client.post("/juniors/ana-python/emparejar", follow_redirects=False).status_code == 303


def test_non_local_hosts_are_rejected(ctx):
    client, app, *_ = ctx
    evil = TestClient(app, base_url="http://evil.example:8765")
    assert evil.get("/buscar").status_code == 403
    assert client.post("/buscar", data=FORM, headers={"Origin": "http://evil.example"}).status_code == 403
    assert not app.state.manager.running


def test_laya_status_partial(ctx, monkeypatch):
    client, *_ = ctx
    assert client.get("/laya?engine=heuristic").text == ""
    monkeypatch.setattr("offerscan.service.laya_available", lambda url: False)
    assert "laya.ps1" in client.get("/laya?engine=laya").text
    monkeypatch.setattr("offerscan.service.laya_available", lambda url: True)
    assert "responde" in client.get("/laya?engine=laya").text


def test_laya_down_blocks_search_before_crawling(ctx, monkeypatch):
    client, _, calls, _ = ctx
    monkeypatch.setattr("offerscan.service.laya_available", lambda url: False)
    resp = client.post("/buscar", data={**FORM, "engine": "laya"})
    assert resp.status_code == 422 and "laya.ps1" in resp.text
    assert calls == []
