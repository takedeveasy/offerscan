# Notas para quien mantiene el código

La guía de uso está en el [README](../README.md). Aquí queda el detalle interno.

Pipeline: `crawl → normalize → dedupe → prefilter → evaluate → rank → match → report`.

Sin claves funciona el motor heurístico. Laya (`scripts/laya.sh` / `scripts/laya.ps1`) es un híbrido local. Jev AI es opcional y de pago (`JEV_API_KEY` en `.env`).

## Configuración

| Fichero | Contenido |
|---|---|
| `.env` | Claves opcionales (Jev, InfoJobs API, Laya) |
| `config/search.yaml` | Portales, prefiltro, `non_dev_title_terms` |
| `config/scoring.yaml` | Umbrales (`junior_target_*`, `junior_salary_max`) |
| `config/juniors/*.yaml` | Perfiles **sin PII** |
| `config/searches/*.yaml` | Búsquedas de la UI (gitignored) |

## Tests y calidad

```text
pip install -e ".[dev,ui]"
pytest                 # offline
pytest --live          # smoke contra portales (no corre por defecto)
offerscan eval --from-db-labels
```

## Portales

| Portal | Notas |
|---|---|
| Tecnoempleo | HTML; filtros de provincia/tecnología en el portal |
| InfoJobs | API si hay `INFOJOBS_CLIENT_ID/SECRET`; si no, HTML + stealth ante captcha |
| LinkedIn | Guest endpoints; **desactivado por defecto** |
| Infoempleo | HTML; provincia en la ruta (`/trabajo/en_<slug>/`), `pagina=N`; detalle por JSON-LD. Con 0 años usa `/primer-empleo/` |
| Trabajos.com | HTML; `/ofertas-empleo/<provincia>/<kw>`, una sola página; detalle en `div.subtitulo` (su JSON-LD es una plantilla vacía) |
| JobFluent | HTML; solo `empleos-{madrid,barcelona,valencia,remoto}` (otras ciudades redirigen a Barcelona); detalle por microdata |
| Manfred | API pública `/api/v2/public/offers` (todas las ofertas activas); stack y tareas del `__NEXT_DATA__` del detalle; filtra por keyword en el spider |
| Indeed | JSON embebido (`mosaic-provider-jobcards`); solo página 1 (la 2 pide login) y sin detalle (401); stealth si bloquea |
| Glassdoor | Tarjetas `li[data-test=jobListing]`; búsqueda nacional (`IN219`), una página; stealth si bloquea |
| Talent.com | Agregador; tarjetas `article.JobCard_card__*`, `p=N`; detalle por JSON-LD |
| Jooble | Agregador; estado Redux `__INITIAL_STATE__`; rutas SEO `/trabajo-<kw>/<Provincia>` (robots.txt prohíbe `/SearchResult`) |
| Himalayas | API JSON `/jobs/api/search?country=Spain&seniority=Entry-level`; solo remoto |
| Remotive | API JSON pública, pero su robots.txt prohíbe `/api/*`: con `robots_txt_obey: true` no descarga nada |

Añadir un portal: fixtures en `tests/fixtures/<portal>/`, parser con `Selector`, clase `@register` que herede `BaseJobSpider` (y `stealth_fallback = True` si el portal bloquea), import en `scrapers/__init__.py`, entrada en `search.yaml`, en `SERVER_SIDE_FILTERS` (`scrapers/runner.py`) y en `PORTAL_GROUPS`/`FILTER_NOTES` (`ui/app.py`). Ayudas en `base.py`: `ld_detail` (JSON-LD JobPosting), `embedded_json`, `slug`, `mentions_keyword`.

## Navegador stealth

`stealth_fallback = True` en un spider (InfoJobs, Indeed, Glassdoor) añade una sesión `AsyncStealthySession` perezosa y reintenta en ella las páginas bloqueadas. Esa sesión lanza el Chromium de **Patchright**, que se instala aparte:

- `offerscan install-browser` (`src/offerscan/browser.py`) ejecuta `python -m patchright install chromium` y comprueba `chromium.executable_path` y un arranque headless real. `--with-deps` añade las librerías de sistema (Linux, root); `--check` solo verifica.
- **No** delegamos en `scrapling install`: instala vía `playwright` y escribe el marcador `scrapling/.scrapling_dependencies_installed`; con el marcador presente ya no reinstala aunque falte el binario o Patchright pida una revisión nueva de Chromium (pasó en este repo: `chromium-1248` ausente con el marcador puesto).
- `offerscan doctor` usa `browser.is_installed()` (ruta exacta que lanzará Patchright, válida en Windows/macOS/Linux y con `PLAYWRIGHT_BROWSERS_PATH`).
- Si un portal con stealth termina con bloqueos/fallos y el navegador falta, `scrapers/runner.py` sustituye el error por «ejecuta `offerscan install-browser`», que la UI muestra en el progreso.
- `scripts/setup.ps1` / `setup.sh` lo instalan al final (no fatal: AVISO si falla). `setup.ps1` aborta si hay un `offerscan` de `.venv` en marcha, porque Windows bloquea el `.exe` y `pip` deja la instalación a medias (dist-info renombrado a `~obdream…` y sin `.pth`). `setup.ps1` va en **UTF-8 con BOM**: PowerShell 5.1 lee los `.ps1` sin BOM como ANSI y rompe las tildes.
- Docker: `RUN … offerscan install-browser --with-deps` en la imagen.

## Otros

- `scripts/schedule.ps1`: tarea diaria en Windows
- `Dockerfile`: imagen `pyd4vinci/scrapling` + `offerscan install-browser --with-deps` (CLI, no la UI)
- Plan original: [PLAN.md](../PLAN.md)
- Marca UI: paleta takedeveasy en `src/offerscan/ui/static/style.css`
