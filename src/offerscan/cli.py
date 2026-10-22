from __future__ import annotations

import importlib.metadata
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from offerscan import browser
from offerscan.settings import Settings, load_search_config
from offerscan.storage.db import Database

app = typer.Typer(help="offerscan: rastrea ofertas junior y las puntua con Jev AI.", no_args_is_help=True)
console = Console()


@app.callback()
def main() -> None:
    """offerscan: rastrea ofertas junior y las puntua con Jev AI."""


def _laya_up(base_url: str) -> bool:
    from offerscan.service import laya_available

    return laya_available(base_url)


@app.command()
def doctor() -> None:
    """Comprueba el entorno (Python, scrapling, navegadores, claves)."""
    settings = Settings()
    table = Table(title="offerscan doctor")
    table.add_column("Comprobacion")
    table.add_column("Estado")
    table.add_column("Detalle")

    def row(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        mark = "[green]OK[/]" if ok else ("[yellow]AVISO[/]" if warn else "[red]FALLO[/]")
        table.add_row(name, mark, detail)

    row("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        row("scrapling", True, importlib.metadata.version("scrapling"))
    except importlib.metadata.PackageNotFoundError:
        row("scrapling", False, "pip install 'scrapling[fetchers,ai,shell]'")
    browser_ok = browser.is_installed()
    row(
        "Navegador stealth (Chromium)",
        browser_ok,
        "listo" if browser_ok else f"falta: ejecuta `{browser.INSTALL_HINT}` (InfoJobs, Indeed y Glassdoor lo usan)",
        warn=True,
    )
    row(
        "JEV_API_KEY",
        settings.has_jev_key,
        "presente" if settings.has_jev_key else "ausente: se usara el motor heuristico",
        warn=True,
    )
    has_ij = bool(settings.infojobs_client_id and settings.infojobs_client_secret)
    row(
        "Credenciales InfoJobs",
        has_ij,
        "API oficial" if has_ij else "ausentes: se usara el fallback HTML (puede bloquearse)",
        warn=True,
    )
    laya_ok = _laya_up(settings.laya_base_url)
    row(
        "Servidor Laya (motor gratuito)",
        laya_ok,
        settings.laya_base_url if laya_ok else "no responde: scripts/laya.ps1 o bash scripts/laya.sh",
        warn=True,
    )
    row("config/search.yaml", (settings.config_dir / "search.yaml").exists())
    console.print(table)


@app.command(name="install-browser")
def install_browser(
    with_deps: Annotated[
        bool, typer.Option("--with-deps", help="Linux: instala tambien las librerias del sistema (necesita sudo/root).")
    ] = False,
    check_only: Annotated[bool, typer.Option("--check", help="Solo comprueba que el navegador arranca.")] = False,
) -> None:
    """Descarga el Chromium de la sesion stealth (portales que bloquean) y comprueba que arranca."""
    if not check_only:
        console.print("Descargando Chromium para Scrapling/Patchright (unos 150-200 MB, solo la primera vez)...")
        if browser.install(with_deps) != 0:
            console.print("[red]La descarga fallo.[/] Revisa la conexion y vuelve a ejecutar el comando.")
            raise typer.Exit(1)
    path = browser.executable_path()
    if path is None or not path.exists():
        console.print(f"[red]No encuentro el navegador[/] ({path}). Ejecuta `{browser.INSTALL_HINT}`.")
        raise typer.Exit(1)
    error = browser.launch_check()
    if error:
        console.print(f"[red]Chromium esta instalado pero no arranca:[/] {error}")
        if sys.platform.startswith("linux"):
            deps = f"sudo {sys.executable} -m patchright install-deps chromium"
            console.print(f"En Linux suelen faltar librerias del sistema: `{deps}` (o `--with-deps` como root).")
        raise typer.Exit(1)
    console.print(f"[green]Navegador listo[/]: {path}")


@app.command()
def crawl(
    sources: Annotated[
        str | None, typer.Option(help="Portales separados por coma (por defecto, los activados).")
    ] = None,
    max_pages: Annotated[int | None, typer.Option(help="Paginas maximas por busqueda.")] = None,
    dev: Annotated[bool, typer.Option("--dev", help="Cachea respuestas en disco (modo desarrollo).")] = False,
) -> None:
    """Rastrea los portales y guarda las ofertas normalizadas."""
    from offerscan.scrapers.runner import crawl as run_crawl

    settings = Settings()
    search_cfg = load_search_config(settings.config_dir)
    names = [s.strip() for s in sources.split(",")] if sources else search_cfg.enabled_sources()
    db = Database(settings.db_path)
    counts: dict[str, int] = {}
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("Rastreando...", total=None)

        def on_item(source: str, n: int) -> None:
            counts[source] = n
            summary = ", ".join(f"{k}={v}" for k, v in counts.items())
            progress.update(task, description=f"Rastreando: {summary}")

        results = run_crawl(names, search_cfg, settings, db, max_pages=max_pages, dev=dev, on_item=on_item)
    table = Table(title="Resultado del rastreo")
    for col in ("Portal", "Peticiones", "Ofertas", "Fallos", "Bloqueos", "Guardadas", "Error"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.source, str(r.fetched), str(r.parsed), str(r.failed), str(r.blocked), str(r.stored), r.error
        )
    console.print(table)
    if any(r.error or (r.parsed == 0 and r.blocked) for r in results):
        console.print("[yellow]Aviso: algun portal fallo o bloqueo el rastreo; el resto continua.[/]")


def _select_juniors(settings: Settings, junior: str | None):
    from offerscan.settings import load_juniors

    juniors = load_juniors(settings.config_dir)
    if junior:
        juniors = [j for j in juniors if j.slug == junior]
        if not juniors:
            console.print(f"[red]No existe el junior '{junior}' en config/juniors/[/]")
            raise typer.Exit(1)
    if not juniors:
        console.print("[red]No hay perfiles en config/juniors/*.yaml[/]")
        raise typer.Exit(1)
    return juniors


def _engine(requested: str | None, settings: Settings) -> str:
    from offerscan.service import resolve_engine

    name, note = resolve_engine(requested, settings)
    if note:
        console.print(f"[yellow]{note}[/]")
    return name


@contextmanager
def _laya_errors():
    from offerscan.ai.jev_client import JevUnreachable

    try:
        yield
    except JevUnreachable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


def _print_eval(prep, stats, engine_name: str) -> None:
    console.print(
        f"Preparadas: {prep.new} nuevas -> {prep.duplicates} duplicadas, "
        f"{prep.prefiltered} descartadas por reglas, {prep.candidates} candidatas."
    )
    console.print(
        f"Evaluacion ({engine_name}): {stats.evaluated} nuevas, {stats.cached} en cache, "
        f"{stats.failed} fallidas, {stats.credits} creditos."
    )
    if stats.stopped_reason:
        console.print(f"[red]Etapa detenida: {stats.stopped_reason}. Las ofertas restantes siguen pendientes.[/]")
        if engine_name == "laya":
            raise typer.Exit(1)


EngineOpt = Annotated[str | None, typer.Option(help="jev | laya | heuristic (defecto: jev si hay clave).")]
JuniorOpt = Annotated[str | None, typer.Option("--junior", help="Slug del junior (por defecto, todos).")]


@app.command()
def evaluate(
    engine: EngineOpt = None,
    limit: Annotated[int | None, typer.Option(help="Maximo de ofertas a evaluar.")] = None,
) -> None:
    """Deduplica, prefiltra y puntua las ofertas con Jev (o el motor heuristico)."""
    from offerscan.service import evaluate_stage

    settings = Settings()
    name = _engine(engine, settings)
    db = Database(settings.db_path)
    with _laya_errors():
        prep, stats = evaluate_stage(db, settings, load_search_config(settings.config_dir), name, limit)
    _print_eval(prep, stats, name)


@app.command()
def match(
    junior: JuniorOpt = None,
    all_: Annotated[bool, typer.Option("--all", help="Todos los juniors.")] = False,
    engine: EngineOpt = None,
) -> None:
    """Empareja las mejores ofertas con cada junior."""
    from offerscan.service import match_stage
    from offerscan.settings import load_scoring_config

    settings = Settings()
    name = _engine(engine, settings)
    db = Database(settings.db_path)
    juniors = _select_juniors(settings, None if all_ else junior)
    with _laya_errors():
        results = match_stage(db, settings, load_scoring_config(settings.config_dir), name, juniors)
    for slug, (matches, stats) in results.items():
        tops = sum(m.bucket.value == "top" for m in matches)
        console.print(
            f"{slug}: {len(matches)} emparejadas ({tops} top), {stats.evaluated} nuevas, "
            f"{stats.cached} en cache, {stats.credits} creditos."
        )
        if stats.stopped_reason:
            console.print(f"[red]{slug}: etapa detenida: {stats.stopped_reason}[/]")


@app.command()
def report(
    junior: JuniorOpt = None,
    all_: Annotated[bool, typer.Option("--all", help="Todos los juniors.")] = False,
    format: Annotated[str, typer.Option(help="html | md | csv | jsonl | all")] = "all",
) -> None:
    """Genera los informes (HTML/MD/CSV/JSONL) por junior en reports/."""
    from offerscan.report.render import FORMATS
    from offerscan.service import report_stage
    from offerscan.settings import load_scoring_config

    settings = Settings()
    formats = list(FORMATS) if format == "all" else [format]
    db = Database(settings.db_path)
    juniors = _select_juniors(settings, None if all_ else junior)
    paths = report_stage(db, load_scoring_config(settings.config_dir), juniors, settings.reports_dir, formats)
    for path in paths:
        console.print(f"Informe: {path}")


@app.command()
def run(
    engine: EngineOpt = None,
    sources: Annotated[str | None, typer.Option(help="Portales separados por coma.")] = None,
    max_pages: Annotated[int | None, typer.Option(help="Paginas maximas por busqueda.")] = None,
    from_fixtures: Annotated[
        Path | None,
        typer.Option(
            "--from-fixtures",
            help="Sin red: usa paginas guardadas (por defecto tests/fixtures).",
            show_default=False,
        ),
    ] = None,
    junior: JuniorOpt = None,
    dev: Annotated[bool, typer.Option("--dev", help="Cache de desarrollo.")] = False,
) -> None:
    """Pipeline completo: crawl -> evaluate -> match -> report."""
    from offerscan.demo import load_fixture_raws
    from offerscan.pipeline.stages import ingest_raw
    from offerscan.scrapers.runner import crawl as run_crawl
    from offerscan.service import evaluate_stage, match_stage, report_stage
    from offerscan.settings import load_scoring_config

    settings = Settings()
    name = _engine(engine, settings)
    search_cfg = load_search_config(settings.config_dir)
    scoring = load_scoring_config(settings.config_dir)
    juniors = _select_juniors(settings, junior)
    db = Database(settings.db_path)

    if from_fixtures is not None:
        raws = load_fixture_raws(from_fixtures)
        console.print(f"[bold]1/4[/] Fixtures: {len(raws)} ofertas leidas sin red desde {from_fixtures}")
        ingest_raw(db, raws, search_cfg)
    else:
        names = [s.strip() for s in sources.split(",")] if sources else search_cfg.enabled_sources()
        console.print(f"[bold]1/4[/] Rastreando: {', '.join(names)}")
        for r in run_crawl(names, search_cfg, settings, db, max_pages=max_pages, dev=dev):
            console.print(
                f"  {r.source}: {r.parsed} ofertas, {r.failed} fallos, {r.blocked} bloqueos {r.error}"
            )

    console.print("[bold]2/4[/] Evaluando")
    with _laya_errors():
        prep, stats = evaluate_stage(db, settings, search_cfg, name)
    _print_eval(prep, stats, name)
    console.print("[bold]3/4[/] Emparejando")
    with _laya_errors():
        match_results = match_stage(db, settings, scoring, name, juniors)
    for slug, (matches, mstats) in match_results.items():
        console.print(f"  {slug}: {len(matches)} emparejadas ({mstats.credits} creditos)")
    console.print("[bold]4/4[/] Informes")
    for path in report_stage(db, scoring, juniors, settings.reports_dir, ["html", "md", "csv", "jsonl"], name):
        console.print(f"  {path}")


@app.command(name="eval")
def eval_(
    file: Annotated[Path, typer.Option(help="JSONL etiquetado.")] = Path("tests/eval/labeled_offers.jsonl"),
    engine: EngineOpt = None,
    from_db_labels: Annotated[
        bool,
        typer.Option("--from-db-labels", help="Usa las etiquetas humanas guardadas en la BD (UI) en vez del JSONL."),
    ] = False,
    export_labels: Annotated[
        Path | None,
        typer.Option("--export-labels", help="Anade las etiquetas de la BD a este JSONL (sin duplicar) y termina."),
    ] = None,
) -> None:
    """Calibra junior_friendly: precision/recall por umbral contra ofertas etiquetadas."""
    from offerscan.evaluation import export_labeled_offers, labeled_rows_from_db, read_labeled, run_eval_rows

    settings = Settings()
    if export_labels is not None:
        added, total = export_labeled_offers(Database(settings.db_path), export_labels)
        console.print(f"{added} etiquetas nuevas anadidas a {export_labels} ({total} en total).")
        return
    if from_db_labels:
        lines = labeled_rows_from_db(Database(settings.db_path))
        if not lines:
            console.print("[red]No hay etiquetas humanas en la BD: marca ofertas como junior / no junior en la UI.[/]")
            raise typer.Exit(1)
    else:
        lines = read_labeled(file)
    name = _engine(engine, settings)
    with _laya_errors():
        rows = run_eval_rows(lines, settings, name)
    table = Table(title=f"Calibracion junior_friendly ({name}, {rows.total} ofertas, {rows.positives} junior)")
    for col in ("Umbral", "Precision", "Recall", "F1", "Predichas"):
        table.add_column(col)
    for r in rows.thresholds:
        table.add_row(f"{r.threshold:.2f}", f"{r.precision:.2f}", f"{r.recall:.2f}", f"{r.f1:.2f}", str(r.predicted))
    console.print(table)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Puerto local.")] = 8765,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="No abre el navegador.")] = False,
) -> None:
    """Interfaz web local (solo 127.0.0.1): buscar, ver resultados y etiquetar ofertas."""
    try:
        import uvicorn

        from offerscan.ui.app import create_app
    except ImportError as exc:
        console.print('[red]Faltan dependencias de la UI. Instala con: pip install -e ".[ui]"[/]')
        raise typer.Exit(1) from exc
    url = f"http://127.0.0.1:{port}"
    console.print(f"offerscan UI en [bold]{url}[/] (Ctrl+C para salir)")
    if not no_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(port=port), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
