# offerscan — Plan de implementación

Herramienta CLI en Python que rastrea ofertas de empleo tech en portales españoles,
las normaliza, descarta las que no son para perfiles junior y usa **Jev AI** para
puntuar y emparejar las mejores ofertas con cada junior. El scraping se hace con
**Scrapling**.

## 1. Objetivos y no-objetivos

**Objetivos (v1)**
- Rastrear LinkedIn (endpoints públicos "guest", sin login), InfoJobs y Tecnoempleo.
- Arquitectura de *adapters* para añadir portales nuevos (Indeed ES, Manfred, Domestika,
  JobFluent, Welcome to the Jungle...) sin tocar el núcleo.
- Filtrado en dos capas: reglas deterministas baratas → decisiones tipadas con Jev.
- Ranking por oferta (¿es buena para un junior?) y *matching* por junior (¿encaja con Ana?).
- Informe HTML/Markdown por junior + export CSV/JSONL.
- Funciona sin `JEV_API_KEY` (scorer heurístico de respaldo) para tests y uso offline.

**No-objetivos (v1)**
- Nada de login en portales ni scraping con cuentas personales (riesgo de baneo y legal).
- Sin aplicar automáticamente a ofertas.
- Sin UI web (solo CLI + informe HTML estático).

## 2. Cómo se exprime cada repositorio

### Scrapling (`scrapling[fetchers,ai,shell]`)
| Capacidad | Uso en el proyecto |
|---|---|
| `Spider` + `configure_sessions` multi-sesión | Un spider por portal; sesión `fast` (`FetcherSession(impersonate="chrome")`) y sesión `stealth` (`AsyncStealthySession`, `lazy=True`) solo para páginas protegidas. |
| `AutoThrottle`, `concurrent_requests`, `download_delay` por dominio | Ritmo educado por portal (config por adapter). |
| `robots_txt_obey` | Activado por defecto; configurable por adapter con aviso explícito en logs. |
| Detección/reintento de bloqueos | Hook `is_blocked` personalizado por portal (captcha, 429, página vacía). |
| `crawldir` (pause/resume) | Crawls largos reanudables (`data/crawl/<portal>`). |
| Development mode (caché a disco) | `--dev` en la CLI para iterar parsers sin volver a golpear los portales. |
| `spider.stream()` | Barra de progreso en tiempo real (rich) durante `crawl`. |
| `ProxyRotator` | Opcional vía `PROXIES` en `.env`. |
| `solve_cloudflare=True` | En el adapter que lo necesite (p.ej. Indeed futuro). |
| `record_requests=True` + `session.network.search()` | Capturar el JSON XHR de portales SPA en vez de parsear HTML. |
| Selección adaptativa (`auto_save` / `adaptive=True`) y `find_similar()` | Parsers resilientes a cambios de maquetación en las tarjetas de ofertas. |
| `page.markdown()` | Descripción limpia y compacta que se envía como `state` a Jev. |
| `Selector` (parser sin red) | Tests de parsers contra fixtures HTML guardados. |
| Exporters (`to_jsonl`) | Dump crudo por ejecución en `data/raw/`. |
| MCP server + Agent Skill | `.cursor/mcp.json` con `scrapling mcp` y skill instalada para que los agentes descubran selectores nuevos al añadir portales. |

### Jev AI (`POST {JEV_API_BASE_URL}/v1/systemone`, `Authorization: Bearer $JEV_API_KEY`)
| Capacidad | Uso en el proyecto |
|---|---|
| `choice` | `seniority`, `role_family`, `work_mode`. |
| `score` | `experience_required`, `offer_quality`, `junior_fit` (por junior). |
| `noul` | `junior_friendly`, `mentorship`, `red_flags`, `stack_match` (por junior). |
| Preguntas paralelas con un mismo `state` | Una sola petición por oferta con todas las preguntas de oferta. |
| `probabilities` / `confidence` | Umbrales configurables; baja confianza → bucket "revisión humana". |
| `usage` / `creditsUsed` | Contabilidad y tope de créditos por ejecución (`JEV_MAX_CREDITS_PER_RUN`). |
| Agent Skill (`jev-agent-skill`) | Copiada a `.agents/skills/jev-agent/SKILL.md` para agentes que mantengan el proyecto. |

Envelope de respuesta: `{"code":0,"message":"ok","data":{"result":{"answers":{...},"usage":{...},"elapsedMs":..},"creditsUsed":..}}`.
Errores: `401` clave inválida, `402` sin créditos, `429`/timeouts/5xx → reintento acotado con backoff,
`code != 0` o sin `data.result.answers` → respuesta inválida (nunca se lee como resultado).

> Jev advierte que la precisión en idiomas distintos del inglés debe validarse. Las
> `instructions` y `criteria` se escriben en **inglés**, el `state` va en el idioma original
> (español), y se incluye un set de evaluación (sección 8) para calibrar umbrales.

## 3. Arquitectura

```
offerscan/
├── pyproject.toml              # paquete `offerscan`, entry point `offerscan`
├── .env.example                # JEV_API_KEY, JEV_API_BASE_URL, JEV_MODEL, INFOJOBS_*, PROXIES...
├── config/
│   ├── search.yaml             # keywords, ubicaciones, portales activos, límites
│   ├── scoring.yaml            # pesos y umbrales del ranking
│   └── juniors/*.yaml          # un fichero por junior (sin datos personales sensibles)
├── data/                       # (gitignored) sqlite, raw jsonl, caché dev, crawldir
├── reports/                    # (gitignored) informes generados
├── src/offerscan/
│   ├── cli.py                  # typer: doctor | crawl | evaluate | match | report | run
│   ├── settings.py             # pydantic-settings (.env) + carga de YAML
│   ├── models.py               # JobOffer, JuniorProfile, OfferDecision, Match (pydantic)
│   ├── storage/db.py           # SQLite (sqlmodel): offers, decisions, matches, runs
│   ├── scrapers/
│   │   ├── base.py             # BaseJobSpider(Spider) + registry de adapters
│   │   ├── linkedin.py
│   │   ├── infojobs.py
│   │   └── tecnoempleo.py
│   ├── pipeline/
│   │   ├── normalize.py        # salario, años de experiencia, remoto, fechas, limpieza
│   │   ├── dedupe.py           # dedupe intra y cross-portal (rapidfuzz)
│   │   └── prefilter.py        # reglas duras deterministas
│   ├── ai/
│   │   ├── jev_client.py       # cliente async httpx, retries, caché, presupuesto
│   │   ├── questions.py        # definiciones versionadas de preguntas (única fuente)
│   │   └── heuristic.py        # scorer de respaldo sin Jev
│   ├── ranking/
│   │   ├── offer_score.py      # puntuación global de la oferta
│   │   └── matcher.py          # matching oferta × junior
│   └── report/
│       ├── render.py           # Jinja2 → HTML/MD, CSV, JSONL
│       └── templates/
├── tests/
│   ├── fixtures/<portal>/*.html|json
│   └── test_*.py
└── .cursor/mcp.json            # servidor MCP de Scrapling
```

Flujo: `crawl → normalize → dedupe → prefilter → evaluate (Jev) → rank → match (Jev, top N) → report`.
Cada etapa persiste en SQLite, así que se puede re-ejecutar cualquier etapa sin repetir las anteriores.

## 4. Modelo de datos

`JobOffer`: `id` (sha1 de `source+external_id`), `source`, `external_id`, `url`, `title`, `company`,
`location`, `work_mode` (onsite/hybrid/remote/unknown), `salary_min`, `salary_max`, `salary_currency`,
`contract_type`, `experience_years_min` (regex), `description_md`, `skills: list[str]`,
`published_at`, `scraped_at`, `raw: dict`, `dedupe_group`.

`OfferDecision`: `offer_id`, `question_set_version`, `model`, `answers` (JSON completo), `credits_used`,
`created_at`, `engine` (`jev` | `heuristic`).

`JuniorProfile` (YAML): `slug`, `stack`, `nice_to_have`, `languages`, `locations`, `work_mode_pref`,
`min_salary`, `experience_months`, `interests`. **Sin nombre completo, email ni teléfono** — eso no
se envía nunca a Jev.

`Match`: `offer_id`, `junior_slug`, `fit_score`, `stack_match`, `final_score`, `bucket`
(`top` | `review` | `discard`), `reasons: list[str]`.

## 5. Adapters de portales

Todos heredan de `BaseJobSpider`, exponen `source_name`, `build_start_requests(search_cfg)`,
`parse_listing`, `parse_detail` y devuelven dicts que `normalize` convierte a `JobOffer`.
**Antes de escribir selectores, inspeccionar el HTML real** (con `scrapling extract` o `Fetcher`)
y guardar fixtures en `tests/fixtures/`.

1. **Tecnoempleo** (el más sencillo; empezar aquí). Listado HTML server-side en
   `https://www.tecnoempleo.com/ofertas-trabajo/` con parámetros de búsqueda. Sesión `fast`.
   Paginación siguiendo el enlace "siguiente". Detalle de oferta para descripción completa.
2. **InfoJobs**. Preferir la **API oficial** (`https://api.infojobs.net/api/9/offer`, Basic auth con
   `INFOJOBS_CLIENT_ID`/`INFOJOBS_CLIENT_SECRET`, filtros por `q`, `province`, `experienceMin`,
   `category=informatica-telecomunicaciones`). Si no hay credenciales, adapter HTML con sesión
   `stealth` + `record_requests=True` para capturar el JSON interno; si el portal bloquea, registrar
   aviso y continuar con los demás portales (nunca abortar la ejecución completa).
3. **LinkedIn** (solo endpoints públicos sin login):
   - Listado: `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=..&location=Spain&f_E=1%2C2&f_TPR=r604800&start=N`
     (`f_E=1` prácticas, `2` entry level; paginación de 10 en 10, tope configurable).
   - Detalle: `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}`.
   - Concurrencia 1–2, AutoThrottle, tope de páginas bajo. `robots_txt_obey` de LinkedIn bloquea estas
     rutas: el adapter está **desactivado por defecto** en `search.yaml` y al activarlo muestra un aviso
     de que su uso es bajo responsabilidad del usuario (Términos de Servicio).

Errores esperables por adapter (bloqueos, cambios de HTML) se registran en `runs` con contadores
`fetched / parsed / failed / blocked`.

## 6. Pipeline determinista

- **normalize.py**: limpieza de texto; salario (`"25.000 - 30.000 €"`, `"25k-30k"`, `"1.800€/mes"`→anual
  ×14 configurable); años de experiencia (`"al menos 2 años"`, `"3+ years"`, `"experiencia mínima: 1 año"`);
  `work_mode` (remoto, teletrabajo, híbrido, presencial); fechas relativas ("hace 3 días").
- **dedupe.py**: clave exacta `source+external_id`; cross-portal con título+empresa normalizados y
  `rapidfuzz.token_set_ratio >= 90` → mismo `dedupe_group` (se conserva la versión más completa).
- **prefilter.py** (antes de gastar créditos): descartar si el título contiene `senior|sr.|lead|principal|
  head of|architect|manager` (lista configurable), si `experience_years_min >= 3`, ofertas > 30 días,
  o empresas en `blacklist`. Guardar el motivo del descarte.

## 7. Capa de decisión con Jev

`questions.py` define `OFFER_QUESTIONS_V1` (una petición por oferta; `state` = JSON con título,
empresa, ubicación, salario, `description_md` truncada a ~6k caracteres):

| id | tipo | contenido |
|---|---|---|
| `seniority` | choice | internship / junior / mid / senior / unclear |
| `role_family` | choice | backend / frontend / fullstack / data_ml / devops_cloud / qa / mobile / other |
| `experience_required` | score | none < under_1y < 1_2y < 3_4y < 5y_plus |
| `junior_friendly` | noul | ¿Contrataría realistamente a alguien con < 1 año de experiencia profesional? |
| `mentorship` | noul | ¿Menciona mentoring, formación, onboarding o plan de carrera? |
| `red_flags` | noul | ¿Señales de alarma: prácticas no remuneradas, falso autónomo, comercial encubierto, requisitos absurdos para junior? |
| `offer_quality` | score | poor < fair < good < excellent (claridad, salario visible, stack concreto, crecimiento) |

`MATCH_QUESTIONS_V1` (solo para el top N por junior, `state` = `{offer resumida, junior profile sin PII}`):

| id | tipo | contenido |
|---|---|---|
| `junior_fit` | score | poor < partial < good < strong |
| `stack_match` | noul | ¿El stack principal del junior cubre los requisitos imprescindibles? |

Formato: `choice` → `criteria` como objeto `{opcion: descripción}`; `score` → `criteria` como array
ordenado de menor a mayor; `noul` → `instructions` (+ `criteria` opcional true/false).
**Antes de implementar, verificar el esquema exacto en `https://thejevai.com/docs`** y ajustar.

`jev_client.py`:
- `httpx.AsyncClient`, timeout 30 s, semáforo de concurrencia (por defecto 4).
- Reintentos con backoff exponencial solo para timeout/429/5xx (máx. 3). `401`/`402` → error claro y
  parada de la etapa (las ofertas quedan pendientes, no se marcan como evaluadas).
- Caché en SQLite por `(offer_id, question_set_version, model)` → nunca se paga dos veces la misma decisión.
- Presupuesto: corta al alcanzar `JEV_MAX_CREDITS_PER_RUN`.
- Nunca loguear la API key; logs con `question ids`, modelo, respuestas y créditos.

`heuristic.py`: implementa la misma interfaz devolviendo respuestas con el mismo shape a partir de
reglas (keywords, años, salario). Se usa si no hay `JEV_API_KEY` o con `--engine heuristic`.

## 8. Ranking, matching y calibración

`offer_score` (0–100), pesos en `scoring.yaml`:
- `junior_friendly` (alto), `1 - red_flags` (alto, y si `red_flags > 0.7` → descarte),
  `seniority ∈ {internship, junior}`, `experience_required` bajo, `offer_quality`, `mentorship`,
  más señales deterministas: salario visible, recencia, `work_mode`.
- Si la `confidence` de `seniority` o `experience_required` < umbral → bucket `review`.

`matcher`: prefiltro determinista por ubicación/modalidad/idioma del junior, intersección de skills
para elegir el top N (por defecto 20) → `MATCH_QUESTIONS_V1` → `final_score = 0.5*offer_score + 0.5*fit`.
`reasons` explica el porqué en lenguaje llano (generado por código a partir de las señales, no por el LLM).

Calibración: `tests/eval/labeled_offers.jsonl` con ~30 ofertas reales etiquetadas a mano
(junior sí/no) y comando `offerscan eval` que calcula precisión/recall de `junior_friendly` por umbral.

## 9. CLI

```
offerscan doctor                         # comprueba Python, scrapling, navegadores, JEV_API_KEY presente (sin mostrarla)
offerscan crawl  [--sources ...] [--dev] [--max-pages N]
offerscan evaluate [--engine jev|heuristic] [--limit N]
offerscan match  [--junior SLUG|--all]
offerscan report [--junior SLUG|--all] [--format html|md|csv]
offerscan run    # todo el pipeline
offerscan eval   # calibración contra el set etiquetado
```

Informe HTML por junior: tabla de top ofertas con puntuación, motivos, badges (remoto, salario,
mentoring), enlace a la oferta, y sección "para revisar a mano".

## 10. Fases de implementación (para Sonnet)

Cada fase termina con `ruff check`, `pytest` en verde y un commit.

- **F0 – Scaffolding**: `git init`, venv, `pyproject.toml` (deps: `scrapling[fetchers,ai,shell]`, `httpx`,
  `pydantic`, `pydantic-settings`, `sqlmodel`, `typer`, `rich`, `pyyaml`, `jinja2`, `rapidfuzz`,
  `python-dateutil`; dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`), `.gitignore`, `.env.example`,
  configs de ejemplo, `offerscan doctor`.
- **F1 – Modelos, settings y storage** + tests.
- **F2 – Normalize / dedupe / prefilter** + tests unitarios exhaustivos de regex (salarios, años).
- **F3 – Scrapers**: base + Tecnoempleo → InfoJobs → LinkedIn. Inspeccionar HTML real, guardar fixtures,
  tests de parsers con `Selector` sin red. Un smoke test `@pytest.mark.live` por portal (no corre por defecto).
- **F4 – Jev**: `questions.py`, `jev_client.py` (tests con `respx`: éxito, 401, 402, 429+retry, `code!=0`,
  caché, presupuesto), `heuristic.py`.
- **F5 – Ranking y matching** + tests.
- **F6 – Reportes y CLI completa** (`run` end-to-end con `--engine heuristic` funcionando sin red usando fixtures).
- **F7 – Extras**: `.cursor/mcp.json` (Scrapling MCP), copiar skill Jev a `.agents/skills/jev-agent/`,
  `README.md` en español (instalación, `.env`, uso, cómo añadir un portal, aviso legal), `Dockerfile`
  basado en `pyd4vinci/scrapling`, script de tarea programada de Windows (`scripts/schedule.ps1`).

## 11. Legal y ética

- Respetar robots.txt y ToS por defecto; LinkedIn desactivado por defecto con aviso.
- Ritmo bajo (AutoThrottle, concurrencia baja), sin login, sin evadir paywalls.
- Solo se almacenan datos de la oferta; no se guardan datos personales de reclutadores.
- Los perfiles de juniors no llevan PII y solo se envía a Jev lo imprescindible.
- Las puntuaciones son una ayuda; la decisión final de a qué aplicar es del junior/mentor.
