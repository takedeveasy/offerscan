# Mejoras v2

Cinco bloques, cada uno en su propio commit y con tests. Orden de implementación = orden del documento.

## 1. Pantalla "Calidad" (calibración con etiquetas humanas)

- Nueva pestaña **Calidad** en la UI. Usa las etiquetas "Sí/No es junior" guardadas en la BD
  (y opcionalmente `tests/eval/labeled_offers.jsonl`).
- Para cada motor con decisiones guardadas (heuristic, laya, jev): nº de etiquetas usadas,
  precisión / cobertura / F1 de la señal junior en una rejilla de umbrales (0,3–0,9), matriz de
  confusión y lista de errores (falsos positivos / negativos con enlace a la oferta).
- Umbral recomendado = mejor F1 con precisión ≥ 0,8 si existe. Botón **"Aplicar umbral"** que
  escribe el valor en `config/scoring.yaml` (con copia `.bak`) y re-evalúa los buckets sin
  volver a llamar al motor.
- Aviso si hay menos de 30 etiquetas o menos de 8 de cada clase: "pocas etiquetas, resultado orientativo".
- Reutilizar la lógica de `offerscan eval` (sin duplicarla).

## 2. Laya: arranque desde la UI, GPU y lotes

- Botón **"Arrancar motor Laya"** en Búsqueda (y en Calidad) cuando no responde: lanza
  `.venv-laya` `laya-serve` como proceso hijo gestionado por la UI (127.0.0.1, `LAYA_MODELS=multilingual`),
  muestra "arrancando…" hasta que `/health` responda, y lo para al cerrar la UI. Si `.venv-laya`
  no existe, indicar el comando de instalación.
- **GPU**: `scripts/laya.ps1 -Gpu` (y `.sh`) instala el build CUDA de PyTorch en `.venv-laya`
  (comprobar con `nvidia-smi`; comando según pytorch.org para la versión instalada) y arranca con
  `LAYA_DEVICE=cuda`. `offerscan doctor` informa del dispositivo real leyendo `/health`
  (`device`, `checkpoint_devices`). Si no hay GPU, CPU como hoy.
- **Lotes**: el motor laya usa `POST /v1/systemone/batch` (hasta 64 estados por llamada) para
  evaluar ofertas. Medir y documentar el tiempo antes/después con las mismas ofertas.

## 3. Seguimiento por junior

- Tabla nueva `applications(junior_slug, offer_id, status, note, updated_at)`.
  Estados: `guardada`, `aplicado`, `entrevista`, `oferta`, `rechazado`, `descartada`.
- En Resultados (cuando se filtra por junior) y en la ficha del junior: selector de estado + nota.
- En la pantalla Juniors: resumen por junior (cuántas en cada estado) y vista tipo tablero
  (columnas por estado) con enlaces a las ofertas.
- Las ofertas con estado no se borran ni se ocultan al re-rastrear.

## 4. Más fuentes de ofertas

- **ATS públicos de empresas** (sin scraping de HTML, APIs JSON públicas):
  - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
  - Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
  - Workable: verificar el endpoint público de empleos de la cuenta antes de implementarlo.
  - `config/company_boards.yaml` con una lista semilla de empresas tech con presencia en España
    que usen esos ATS. **Verificar cada token/slug con una petición real** y dejar solo los que
    respondan con ofertas; filtrar por ubicación España/remoto-España.
  - Un adapter genérico por ATS con el contrato de los adapters existentes.
- **Manfred** y **JobFluent**: inspeccionar las páginas reales; implementar el adapter si el
  contenido es accesible (HTML o JSON interno) y respeta robots.txt; si no, documentar por qué no.
- Todas las fuentes nuevas seleccionables en la UI (Portales) y en `search.yaml`.

## 5. Preparar para publicar

- `LICENSE` MIT (cabecera con "offerscan contributors", año 2026). Revisar compatibilidad
  de dependencias (Scrapling BSD-3, Laya Apache-2.0) y mencionarlo en el README.
- GitHub Actions: `ruff check` + `pytest` (sin tests live) en Windows y Ubuntu, Python 3.11 y 3.12.
  Sin descarga de navegadores ni de Laya en CI.
- Instalación multiplataforma: `scripts/setup.ps1` y `scripts/setup.sh` (venv, `pip install -e ".[ui]"`,
  `scrapling install`, copia de `.env`), y `scripts/laya.sh` equivalente a `laya.ps1`.
- `docker-compose.yml` con dos servicios: `ui` (imagen basada en la de Scrapling, puerto 8765) y
  `laya` (puerto 8000), volúmenes para `data/`, `config/` y `reports/`. La UI dentro de Docker debe
  escuchar en 0.0.0.0 solo dentro del contenedor y publicarse en 127.0.0.1 del host.
- README: sección "Instalación rápida" por sistema operativo, aviso legal de scraping, cómo contribuir
  (añadir un portal), y capturas actualizadas.
- Revisar que nada personal quede versionado (`data/`, `reports/`, `.env`, perfiles reales).
