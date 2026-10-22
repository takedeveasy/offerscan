# Integración de Laya como motor de decisión gratuito

Estudio del 21-10-2026. Laya 0.4.12 (Apache 2.0, Convai Innovations), probado en local
(Windows, CPU) contra ofertas reales de nuestra base de datos.

## 1. Resultado de las pruebas

**Técnicamente encaja.** `laya-serve` implementa `POST /v1/systemone` con el mismo formato
de petición que Jev. Diferencias con nuestro cliente actual:

| Aspecto | Jev | Laya (`laya-serve`) |
|---|---|---|
| Envoltorio de respuesta | `{code, message, data: {result: {answers, usage}, creditsUsed}}` | `{model, answers, usage, routing}` (sin envoltorio) |
| `score` | — | `score` = índice (float), `probabilities` con claves `"0".."k-1"`, `legend` |
| Confianza comparable con Jev | `confidence` | `x_jev_confidence` (su `confidence` es entropía, no comparable) |
| Ventana de texto | 32k tokens | 1024 por defecto en el modelo multilingüe; ampliable con `max_len` (hasta 8192) |
| Coste | créditos | 0 (local) |
| Latencia (CPU, 7 preguntas) | — | 1,3 s (oferta corta) a 10 s (oferta de ~1100 tokens) |

**Calidad por tipo de pregunta** (6 ofertas reales etiquetadas a mano, muestra pequeña):

| Pregunta | Resultado |
|---|---|
| `seniority` (choice) | Bien: internship/junior/senior coherentes |
| `role_family` (choice) | Mal: Data Science → devops_cloud, Oracle DB → fullstack |
| `noul` (junior_friendly, mentorship, red_flags) | **Inservible**: valores altos (0,8–1,0) en casi cualquier oferta larga, incluso invertidos (senior = 0,98 "apta para junior"). Añadir `criteria` true/false no lo arregla |
| `choice` "¿Para quién es adecuada?" en **español** (junior / con_experiencia / no_claro) | **6/6 correctas** |
| Misma pregunta en inglés | 4/6 |
| `red_flags` como choice | Mal (Oracle DB → "unpaid" 0,81) |

Conclusión: Laya sirve para las decisiones de **seniority/aptitud junior** si se formulan como
`choice` en español. El resto (familia de rol, mentoring, señales de alarma) se queda en el
motor heurístico, que ya lo hace con reglas.

## 2. Diseño propuesto

Nuevo motor `--engine laya` = **híbrido**: Laya responde las preguntas en las que es fiable y
`heuristic.py` el resto, devolviendo el mismo shape de respuestas para que `ranking/` no cambie.

1. **Cliente** (`ai/jev_client.py`): aceptar también la respuesta sin envoltorio
   (`answers` en la raíz); preferir `x_jev_confidence` cuando exista; `creditsUsed` = 0 si falta.
   Enviar `max_len` (configurable, por defecto 4096) solo cuando el motor es Laya.
2. **Preguntas** (`ai/questions.py`): `LAYA_QUESTIONS_V1` con `seniority` y `junior_fit_level`
   (choice en español) y `experience_required` (score). Versión de preguntas distinta para que
   la caché no mezcle decisiones de Jev, Laya y heurística.
3. **Motor híbrido** (`ai/evaluate.py`): combinar respuestas Laya + heurística por id de pregunta.
   El matching (`junior_fit`, `stack_match`) se queda en heurística hasta medirlo.
4. **Settings / `.env`**: `LAYA_BASE_URL=http://127.0.0.1:8000`, `LAYA_MODEL=multilingual`,
   `LAYA_MAX_LEN=4096`. Sin clave.
5. **Arranque**: `scripts/laya.ps1` que levanta `laya-serve` desde `.venv-laya` con
   `LAYA_MODELS=multilingual`, `LAYA_HOST=127.0.0.1`. `offerscan doctor` comprueba `GET /health`.
   Laya va en un venv separado porque PyTorch pesa ~1 GB y el resto de la app no lo necesita.
6. **Calibración**: ampliar `tests/eval/labeled_offers.jsonl` a ≥ 60 ofertas **reales** etiquetadas
   por una persona y comparar con `offerscan eval` los motores `heuristic` y `laya`. Activar Laya
   por defecto solo si mejora al heurístico.
7. **Tests**: respuestas Laya grabadas como fixtures (sin envoltorio, score por índice), mocks con
   `respx`, test del motor híbrido.

## 3. Mejoras opcionales

- **GPU (RTX 3060)**: instalar en `.venv-laya` el build CUDA de PyTorch (comando según
  https://pytorch.org/get-started/locally/) y `LAYA_DEVICE=cuda`.
  En CPU ya es usable (~1–2 min para 30–40 ofertas).
- **`/v1/systemone/batch`**: hasta 64 ofertas por llamada.
- **`min_confidence`**: Laya marca `low_confidence` en las respuestas dudosas → bucket "revisar a mano".
- **Fine-tuning**: Laya admite ajuste fino con ejemplos etiquetados; tendría sentido cuando haya
  unos cientos de ofertas etiquetadas.

## 4. Cómo reproducir la prueba

```powershell
python -m venv .venv-laya
.\.venv-laya\Scripts\python.exe -m pip install "laya[serve]"
$env:LAYA_MODELS="multilingual"; $env:LAYA_HOST="127.0.0.1"
.\.venv-laya\Scripts\laya-serve.exe     # http://127.0.0.1:8000, primera vez descarga ~650 MB
```
