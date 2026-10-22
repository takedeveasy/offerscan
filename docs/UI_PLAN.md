# Frontal local + corrección del filtro junior

Objetivo: que cualquiera que clone el repo pueda ejecutar `offerscan ui`, elegir filtros en el
navegador (tecnologías, provincias, portales...), lanzar el rastreo y ver solo ofertas
realmente junior. Todo local, sin login ni hosting.

## Parte A — Corrección del filtro junior

Caso real que lo motiva: "Frontend Developer (React) - Remoto" (MELT GROUP, InfoJobs,
45.000–66.000 €) salió en "Mejores ofertas". Causas medidas:

1. InfoJobs por HTML no trae descripción (0 caracteres): se decide solo con el título.
2. Laya respondió `junior_target = con_experiencia (0,63)` pero el bucket solo mira `seniority`
   (que dijo junior 0,78). `junior_target` es la pregunta validada (6/6) y no se usa como filtro.
3. No hay regla de salario: 45–66k no es salario junior en España.
4. Se cuelan puestos que no son de programación ("Analista funcional CONTROL M",
   "Consultor Cegid PeopleNet", "Administrativo/a de almacén – Carretillero/a").

Cambios:

- **A1. `junior_target` manda** (motor laya): `con_experiencia` con P ≥ umbral → `discard`;
  `no_claro` o P(junior) < umbral → `review`. Umbrales en `config/scoring.yaml`.
  El motor heurístico debe producir una señal equivalente (`junior_target` derivado de reglas)
  para que el ranking trate igual a ambos motores.
- **A2. Tope salarial junior**: `junior_salary_max` (por defecto 35.000 €/año) en `scoring.yaml`.
  `salary_min` > tope → `discard` ("salario de perfil con experiencia"); entre 0,85×tope y tope → `review`.
- **A3. Sin descripción no hay `top`**: descripción < 200 caracteres → como mucho `review` con nota
  "sin descripción, revisar en el portal".
- **A4. InfoJobs**: restringir la búsqueda HTML a la categoría Informática y telecomunicaciones y
  abrir el detalle de las ofertas (sesión stealth, limitado por `max_details`) para obtener descripción.
- **A5. Puestos que no son de programación**: lista configurable `non_dev_title_terms` en `search.yaml`
  (analista funcional, consultor/a funcional, consultor + ERP: sap|odoo|cegid|peoplenet|dynamics|salesforce,
  técnico/a de sistemas, helpdesk/soporte, administrativo, carretillero, comercial…) aplicada en el
  prefiltro, insensible a acentos y género.
- **A6. Tests de regresión** con esos casos reales (MELT, Cegid, Carretillero, Analista funcional).

## Parte B — Frontal local (`offerscan ui`)

**Stack**: FastAPI + Jinja2 (ya usado en informes) + HTMX + CSS ligero sin build (p. ej. Pico.css
servido localmente, sin CDN). Nada de Node. `offerscan ui` arranca en `127.0.0.1:8765` (solo
localhost) y abre el navegador.

**Pantalla 1 — Búsqueda** (formulario):
- Tecnologías: multiselección con sugerencias (python, java, javascript, typescript, react, angular,
  vue, node, php, .net, c#, go, kotlin, swift, sql, data, devops, qa…) + texto libre.
- Provincias: multiselección con las 50 provincias españolas + "Remoto". Mapear a los parámetros de
  cada portal (Tecnoempleo e InfoJobs usan sus propios ids/slugs de provincia: verificarlos en el HTML real).
- Portales: Tecnoempleo, InfoJobs, LinkedIn (este con aviso legal y desmarcado por defecto).
- Modalidad: presencial / híbrido / remoto.
- Experiencia máxima (0, 1, 2 años), antigüedad máxima (7/15/30 días), salario mínimo y tope junior.
- Motor: heurístico / Laya (si Laya no responde, el selector lo indica y sugiere `scripts/laya.ps1`).
- Profundidad: páginas por portal.
- Botón "Buscar": lanza crawl → evaluate en segundo plano; progreso en vivo por portal
  (HTMX polling). Solo una ejecución a la vez. Los filtros elegidos se guardan como "búsqueda
  guardada" (YAML en `config/searches/`) para repetirla.

**Pantalla 2 — Resultados**:
- Tabla con: puntuación, bucket (Mejores / Revisar / Descartadas, con pestañas), título con enlace,
  empresa, ubicación, modalidad, salario, portal, fecha, tecnologías, motivos.
- Filtros instantáneos sobre lo ya guardado (sin volver a rastrear): texto, tecnologías,
  provincia, portal, modalidad, bucket, salario.
- Acciones por oferta: "No es junior" / "Sí es junior" → se guarda como etiqueta humana en la BD y
  alimenta `tests/eval`/`offerscan eval` (así los mentores construyen el set de calibración usando la app).
- Exportar CSV de la vista filtrada.

**Pantalla 3 — Juniors**: listar/crear/editar perfiles (`config/juniors/*.yaml`) con los mismos
selectores, y ver las ofertas emparejadas de cada uno.

**Arquitectura**: la UI llama a las mismas funciones de `service.py` que la CLI (sin duplicar lógica).
Los filtros de búsqueda se convierten en un `SearchConfig` en memoria; los adapters de portal reciben
tecnologías/provincias en vez de leer solo `search.yaml`.

**Calidad**: tests con `fastapi.testclient` (formularios, arranque de búsqueda con crawl simulado,
filtros, etiquetado), README actualizado ("Uso con interfaz web") con capturas, y `offerscan ui`
documentado como forma principal de uso para quien clone el repo.
