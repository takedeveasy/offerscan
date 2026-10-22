"""One background search at a time, with progress that the UI polls.

The crawl (Scrapling, browsers, asyncio.run) runs in its own thread so the web event loop never blocks.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from offerscan.ai.jev_client import JevError, JevUnreachable
from offerscan.scrapers.runner import SourceResult
from offerscan.service import SearchFilters, SearchHooks, SearchSummary

Runner = Callable[[SearchFilters, SearchHooks], SearchSummary]

PENDING, RUNNING, DONE, ERROR = "pendiente", "rastreando", "hecho", "error"


@dataclass
class PortalProgress:
    name: str
    status: str = PENDING
    items: int = 0
    stored: int = 0
    filtered: int = 0
    blocked: int = 0
    failed: int = 0
    error: str = ""


@dataclass
class RunState:
    status: str = "idle"  # idle | running | done | error
    stage: str = ""
    portals: dict[str, PortalProgress] = field(default_factory=dict)
    evaluated: int = 0
    summary: list[str] = field(default_factory=list)
    error: str = ""
    stopped: bool = False
    filters: SearchFilters | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunManager:
    def __init__(self, runner: Runner):
        self._runner = runner
        self._lock = threading.Lock()
        self._state = RunState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state.status == "running"

    def snapshot(self) -> RunState:
        with self._lock:
            return deepcopy(self._state)

    def stop(self) -> bool:
        """Ask the running search to stop at the next offer; False when nothing is running."""
        with self._lock:
            if self._state.status != "running":
                return False
            self._stop.set()
            self._state.stage = "Deteniendo"
        return True

    def clear(self) -> None:
        """Forget the last finished run so the progress panel starts blank; a running search is left alone."""
        with self._lock:
            if self._state.status != "running":
                self._state = RunState()

    def start(self, filters: SearchFilters) -> bool:
        """False when a search is already running."""
        with self._lock:
            if self._state.status == "running":
                return False
            self._stop.clear()
            self._state = RunState(
                status="running",
                stage="Preparando",
                portals={s: PortalProgress(s) for s in filters.sources},
                filters=filters,
                started_at=datetime.now(timezone.utc),
            )
        self._thread = threading.Thread(target=self._run, args=(filters,), name="offerscan-search", daemon=True)
        self._thread.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # --- hooks (called from the worker thread)
    def _update(self, fn: Callable[[RunState], None]) -> None:
        with self._lock:
            fn(self._state)

    def _hooks(self) -> SearchHooks:
        def stage(name: str) -> None:
            self._update(lambda s: setattr(s, "stage", name))

        def started(name: str) -> None:
            self._update(lambda s: setattr(s.portals.setdefault(name, PortalProgress(name)), "status", RUNNING))

        def item(name: str, n: int) -> None:
            self._update(lambda s: setattr(s.portals.setdefault(name, PortalProgress(name)), "items", n))

        def done(result: SourceResult) -> None:
            def apply(s: RunState) -> None:
                p = s.portals.setdefault(result.source, PortalProgress(result.source))
                p.status = ERROR if result.error else DONE
                p.items, p.stored, p.filtered = result.parsed, result.stored, result.filtered
                p.blocked, p.failed, p.error = result.blocked, result.failed, result.error

            self._update(apply)

        def evaluated(n: int) -> None:
            self._update(lambda s: setattr(s, "evaluated", n))

        return SearchHooks(stage, started, done, item, evaluated, should_stop=self._stop.is_set)

    def _run(self, filters: SearchFilters) -> None:
        try:
            summary = self._runner(filters, self._hooks())
        except JevUnreachable as exc:
            self._finish("error", error=str(exc))
        except JevError as exc:
            self._finish("error", error=f"Error del motor de puntuación: {exc}")
        except Exception as exc:  # the worker must never die silently
            self._finish("error", error=f"{type(exc).__name__}: {exc}")
        else:
            self._finish("done", summary=describe(summary), stopped=summary.stopped)

    def _finish(
        self, status: str, error: str = "", summary: list[str] | None = None, stopped: bool = False
    ) -> None:
        def apply(s: RunState) -> None:
            s.stopped = stopped
            s.status, s.error, s.stage = status, error, "Listo" if status == "done" else "Error"
            s.summary = summary or []
            s.finished_at = datetime.now(timezone.utc)

        self._update(apply)


def describe(summary: SearchSummary) -> list[str]:
    """Spanish lines for the end-of-run summary."""
    lines = [f"Motor de puntuación: {summary.engine}."]
    if summary.stopped:
        lines.append("Las ofertas ya rastreadas se han guardado; se puntuarán en la próxima búsqueda.")
    lines += summary.notes
    prep, ev = summary.prepared, summary.evaluation
    if prep is not None:
        lines.append(
            f"{prep.new} ofertas nuevas: {prep.duplicates} duplicadas, {prep.prefiltered} descartadas por reglas "
            f"(título, experiencia, antigüedad, puestos que no son de programación), {prep.candidates} candidatas."
        )
    if ev is not None:
        lines.append(
            f"Evaluadas {ev.evaluated} ofertas nuevas ({ev.cached} ya estaban en caché, {ev.failed} fallidas)."
        )
        if ev.stopped_reason:
            lines.append(f"Evaluación detenida: {ev.stopped_reason}")
    for slug, n in summary.matches.items():
        lines.append(f"Junior {slug}: {n} ofertas emparejadas.")
    return lines
