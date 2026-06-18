# AGENTS.md — Guía operativa para agentes en este repo

## Qué es este proyecto

Pipeline que scrappea el Reglamento Estudiantil de Pregrado de la Universidad de Antioquia desde [normativa.udea.edu.co](https://normativa.udea.edu.co), descarga los PDFs y los vectoriza en [Qdrant Cloud](https://cloud.qdrant.io) con chunking semántico por ideas (marcadores legales españoles) para alimentar una capa RAG.

**Estado verificado:** 28 PDFs scrapeados → 2 con texto extraíble → **766 chunks** ya pusheados a Qdrant Cloud en la colección `udea_reglamento_pregrado`.

Dos scripts:
- `scrape.py` — descarga PDFs + genera Excel + `data.json`
- `vectorize.py` — extracción → chunking semántico → embeddings → upsert

---

## A. Conocimiento de webscraping

### A.1. Estructura del portal

- URL base: `https://normativa.udea.edu.co`
- Tabla de búsqueda filtrable en `/Documentos`. El filtro **"REGLAMENTO ESTUDIANTIL DE PREGRADO"** devuelve **28 resultados en una sola página** — no hay paginación.
- Para cada fila hay que llamar `POST /Documentos/ExtensionDocumento` con `codigodocumento=<docId>` para obtener la extensión (`pdf` o `doc`) y el `codigoimagen`.
- Después `GET /Documentos/Documento?codigodocumento=<docId>&codigoimagen=<codigoimagen>&buscarpdf=` devuelve el binario.
- **Verificar magic bytes `%PDF`** antes de guardar (no fiarse del Content-Type).

### A.2. Metadata scrapeada (`data.json`)

Por registro:

| Campo | Tipo | Notas |
|---|---|---|
| `numero` | str | Identificador "humano" del acuerdo (no único: hay 3 docs con `numero="1"`) |
| `docId` | str | **ID único**. Usar siempre este como clave |
| `fechaExpedicion` | str (YYYY/MM/DD) | Fecha de expedición original |
| `entradaVigencia` | str (YYYY/MM/DD) | A veces vacío |
| `medioPublicacion` | str | A veces vacío; `NORMATIVA.UDEA.EDU.CO` para los modernos |
| `resuelve` | str | Resumen de qué hace el acuerdo. Útil para embeddings/título |
| `normasRelacionadas` | str | Acuerdos anteriores que modifica (ej. `ACUERDO SUPERIOR 01 DE 1981`). **Es la pista del grafo de modificaciones.** |
| `archivo` | str | Nombre del PDF: `Acuerdo_<numero>_<fecha>_<docId>.pdf` |
| `tamano_bytes` | int | Tamaño descargado (sanity check) |
| `url_pdf` | str | URL de descarga (para re-scrapear) |

### A.3. Convenciones de nombres de archivo

`Acuerdo_<numero>_<fecha>_<docId>.pdf`. Regex de parseo: `Acuerdo_(?P<numero>[^_]+)_(?P<fecha>\d{4}-\d{2}-\d{2})_(?P<docId>\d+)\.pdf`.

**Sanitización**: caracteres no-`\w-.` → `_`, espacios colapsados, máximo 120 chars. No usar números con padding en el filename porque `numero` puede tener ceros (`0425`) o no (`623`).

### A.4. Particularidades de UDEA

- Hay **3 versiones del Acuerdo 1 de 1981** (mismo `numero=1`, distinto `docId`):
  - `31878551` — versión original escaneada (pypdf → 0 chars)
  - `35187147` — versión actualizada SIN concordancias (Secretaría General re-tipeó) → **370 chunks**
  - `35187099` — versión actualizada CON concordancias (las modificaciones intercaladas) → **396 chunks**
- **Los 2 maestros re-tipeados cubren ~95% del contenido regulatorio** porque la Secretaría General ya consolidó todas las modificaciones posteriores (0031, 0038, 0118, 0137, 0146, 0164, 0165, 0170, 0177, 0180, 0181, 0225, 0229, 0239, 0267, 0376, 0385, 0425, 0623) en una sola versión actualizada.
- **26 de los 28 PDFs son escaneados** (cada modificación individual). Solo los 2 maestros tienen texto extraíble.
- `resuelve` está en mayúsculas sostenidas en el `data.json`. Si lo muestras en UI, normaliza a Title Case para legibilidad.

### A.5. Anti-patrones del scrapeo

- **No** uses `numero` como clave para deduplicar — colisiona en el caso de las 3 versiones del Acuerdo 1.
- **No** confíes en Content-Type para verificar PDFs — verifica magic bytes.
- **No** hagas `requests.get(url)` sin la sesión que ya tiene headers de User-Agent y `Accept-Language: es-CO` — algunos endpoints devuelven HTML de error sin esos headers.

---

## B. Conocimiento de vectorización y chunking

### B.1. Modelo de embedding

**Usar:** `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (fastembed local, 384 dim).

**NO usar:** `intfloat/multilingual-e5-small` — **no está soportado por fastembed**, lanza `ValueError`. La alternativa multilingüe soportada es la de arriba.

Si necesitas mejor calidad, opciones que sí soporta fastembed (verificar con `TextEmbedding.list_supported_models()`):
- `BAAI/bge-small-en-v1.5` — solo inglés, 384 dim
- `sentence-transformers/all-MiniLM-L6-v2` — solo inglés, 384 dim
- `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` — multilingüe, 768 dim (más pesado)

Si los anteriores no son suficientes en español, sal a OpenRouter con `OPENROUTER_API_KEY=...` y `OPENROUTER_EMBED_MODEL=openai/text-embedding-3-small` (1536 dim).

### B.2. Estrategia de batching para Qdrant Cloud free tier

**Configuración que funciona (verificada, ~10 puntos/s sostenido):**

```python
QdrantClient(url=..., api_key=..., timeout=300, check_compatibility=False)
# upsert en batches de 16 puntos
# wait=True (espera al indexing antes de continuar)
# 8 reintentos con backoff exponencial 3-45s
```

**Lo que NO funciona:**
- `wait=False` con batches grandes (64+): cada batch tarda 60-90s en aceptar, los reintentos queman ~5 min por batch.
- `timeout=60`: muy corto para free tier, cualquier pico de latencia rompe el flujo.
- Batches > 32 puntos: throttling per-request del free tier.

### B.3. Punto ID — bug crítico a recordar

**Qdrant solo acepta IDs `unsigned integer` o `UUID`**, NO strings. Strings como `"35187147:0000"` devuelven **400 Bad Request** que el cliente reporta como timeout genérico (sin mostrar el body).

**Solución determinista e idempotente:**

```python
import uuid
NS = uuid.UUID("00000000-0000-0000-0000-000000000001")
def make_point_id(doc_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(NS, f"{doc_id}:{chunk_index:04d}"))
```

`uuid5` es determinista: misma `(doc_id, chunk_index)` → mismo UUID. Re-upserts actualizan in-place (no duplicados).

### B.4. Versión de qdrant-client

- `qdrant-client==1.10+`: usar `client.query_points()` (la API nueva)
- `qdrant-client<1.10`: usar `client.search()` (deprecated)
- **NO** mezclar versiones — el cliente 1.16 contra servidor 1.18 funciona pero emite warning; el server 1.10 contra cliente 1.16 falla en algunas operaciones.

Para silenciar el warning de versión: `check_compatibility=False` en el constructor.

### B.5. Payload indexado vs. no indexado

Campos como `source_pdf`, `normas_relacionadas`, `numero`, `fecha_expedicion`, `section_marker` se guardan en el payload y son filtrables, **pero solo después de crear un índice**:

```python
client.create_payload_index(
    collection_name="udea_reglamento_pregrado",
    field_name="source_pdf",
    field_schema=qm.PayloadSchemaType.KEYWORD,
)
```

Sin índice, un `qm.Filter` sobre `source_pdf` devuelve **400 Bad Request**. Los campos `keyword` (string exacto) y `integer` son los más comunes.

### B.6. Colección — creación

```python
qm.VectorParams(size=384, distance=qm.Distance.COSINE)
qm.OptimizersConfigDiff(default_segment_number=2)
```

`default_segment_number=2` ayuda a que el HNSW arranque a indexar antes de tener todos los puntos (en free tier el warmup inicial puede tardar minutos).

### B.7. Chunker — invariantes que NO romper

Los chunks **deben** seguir estos invariantes para que el RAG funcione:

1. **1 chunk ≈ 1 idea legal**: el regex parte en marcadores estructurales, NO en caracteres fijos (`CharacterTextSplitter` destruye la estructura).
2. **`section_marker` debe ser el encabezado del marcador** (ej. `ARTÍCULO 5.  [articulo]`) — la UI lo muestra para que el usuario vea de qué norma viene el match.
3. **Overlap con `(...continuación)`** en chunks con índice > 0 — sin esto, las búsquedas que caen a mitad de un artículo pierden el contexto del inicio.
4. **Chunks vacíos no se suben** — el script los filtra con `if txt.strip()`.

Si tocas `chunk_by_ideas()`, valida con `--dry-run` antes de re-vectorizar.

### B.8. Métricas reales del pipeline

| PDF | chars | chunks | embed_secs | upsert_secs |
|---|---|---|---|---|
| Acuerdo 1 de 1981 (CON concordancias) | 198,221 | 396 | 7.06 | 435 |
| Acuerdo 1 de 1981 (SIN concordancias) | 121,156 | 370 | 7.24 | 328 |

Embedding es ~50 chunks/s en CPU (M-series Mac). Upsert a Qdrant Cloud free tier es ~10 puntos/s (limitado por el server, no por el cliente).

---

## C. Convenciones del repo

- **Python 3.9** en `.venv` (venv existente).
- `data.json` es la fuente de verdad de metadata — no editar a mano.
- `.env` está en `.gitignore`. **Nunca commitear** `QDRANT_API_KEY` ni `OPENROUTER_API_KEY`.
- Nombres de PDF: `Acuerdo_<numero>_<fecha>_<docId>.pdf`. No renombrar a mano.

---

## D. Cómo correr el pipeline

### Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Variables de entorno

`cp .env.example .env` y rellenar `QDRANT_URL`, `QDRANT_API_KEY`. Opcional: `OPENROUTER_API_KEY` para embeddings por API.

### Validar chunker sin gastar embeddings

```bash
python vectorize.py --dry-run
python vectorize.py --dry-run --limit 3
```

### Pushear

```bash
python vectorize.py              # los 28 PDFs (skip los 26 escaneados)
python vectorize.py --limit 5    # solo los 5 primeros (test)
python vectorize.py --ocr        # auto-OCR para PDFs sin texto
python vectorize.py --ocr-only   # forzar OCR en todos
```

Re-ejecuciones son idempotentes vía UUID5.

### Borrar y recrear la colección (si cambias el modelo de embedding)

```python
from dotenv import load_dotenv; import os; from qdrant_client import QdrantClient
load_dotenv()
client = QdrantClient(url=os.environ["QDRANT_URL"],
                      api_key=os.environ["QDRANT_API_KEY"],
                      check_compatibility=False)
client.delete_collection("udea_reglamento_pregrado")
# Luego re-correr python vectorize.py (re-crea con la dim del primer embed)
```

---

## E. Puntos de extensión

| Si necesitas... | Edita... |
|---|---|
| Cambiar los marcadores legales reconocidos | `SECTION_PATTERNS` en `vectorize.py` |
| Cambiar el tamaño objetivo / overlap | `TARGET_CHUNK_CHARS`, `OVERLAP_CHARS`, `MIN_CHUNK_CHARS` en `vectorize.py` |
| Cambiar el modelo de embeddings | `DEFAULT_FASTEMBED_MODEL` o `OPENROUTER_EMBED_MODEL` (vía env) |
| Cambiar la colección destino | `--collection` en CLI o `QDRANT_COLLECTION_NAME` en `.env` |
| Añadir OCR para PDFs escaneados | `brew install tesseract tesseract-lang poppler && pip install pytesseract pdf2image`, luego `python vectorize.py --ocr` |
| Cambiar la métrica de distancia Qdrant | `ensure_collection()` — `qm.Distance.COSINE` por defecto |
| Cambiar batch size o esperar el indexing | `upsert_batched()` en `vectorize.py` |
| Cambiar namespace UUID (si migras a otro proyecto) | `_CHUNK_NAMESPACE` en `vectorize.py` — **cambiar esto invalida la idempotencia** |

---

## F. Debugging

| Síntoma | Causa probable | Fix |
|---|---|---|
| Script aborta con "Faltan QDRANT_URL..." | `.env` vacío o sin esas vars | Rellenar `.env` |
| 26 PDFs saltados con `text extracted = 0 chars` | PDFs escaneados | Instalar tesseract y usar `--ocr` |
| 1 solo chunk por PDF | PDF escaneado | Igual al anterior |
| Todos los chunks con label `(sin marcador)` | Regex no detecta los marcadores | Inspeccionar texto crudo con `pypdf`, ajustar `SECTION_PATTERNS` |
| `[retry] batch N attempt 8` repetido y luego timeout | IDs string en vez de UUID/int | Verificar `make_point_id` retorna UUID5 |
| `The write operation timed out` cada 60s | `timeout=60` muy corto o batch muy grande | Subir timeout a 300, bajar `batch_size` a 16, usar `wait=True` |
| `Bad request: Index required but not found for "X"` | Filter sin índice | Crear índice con `client.create_payload_index(...)` |
| `Format error in JSON body: value X is not a valid point ID` | ID string con caracteres no-UUID | Usar `uuid.uuid5(NS, ...)` |
| `client.search` no existe / AttributeError | qdrant-client ≥ 1.10 | Usar `client.query_points(...)` |
| `ValueError: Model intfloat/multilingual-e5-small is not supported` | fastembed no soporta ese modelo | Cambiar a `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| `UserWarning: The model … now uses mean pooling instead of CLS embedding` | Cambio de default en fastembed | Es solo un warning, los vectores son válidos |
| `Qdrant client version 1.16 is incompatible with server version 1.18` | Versiones difieren | Pasar `check_compatibility=False` al cliente |
| Embed_secs = 7 pero el modelo nunca se descargó | Primer uso descarga a `~/.cache/huggingface/` | Esperar; siguiente corrida es instantánea |

---

## G. Archivos importantes

- `vectorize.py` — chunker + embedder + upsert. **Aquí pasarás el 90% del tiempo.**
- `scrape.py` — solo tocar si cambia normativa.udea.edu.co.
- `data.json` — no editar a mano (fuente de verdad del scrape).
- `.env` — no commitear.
- `pdfs/` — los originales scrapeados; no modificar.

---

## H. Lo que NO hacer

- **No subir PDFs completos como 1 punto cada uno** — son docs de 50+ páginas, no se buscan bien.
- **No usar `chunk_size` fijo tipo LangChain `CharacterTextSplitter`** — destruye la estructura legal.
- **No usar embeddings en inglés para textos en español** — usar modelos multilingües.
- **No usar IDs de punto string** en Qdrant — solo UUID o unsigned int.
- **No usar `wait=False` con batches grandes** en Qdrant Cloud free tier — timeouts largos queman retries.
- **No borrar `.venv/` ni `pdfs/` sin confirmar** — `.venv` toma tiempo en recrear; `pdfs/` requiere re-scraping.
- **No confiar en `client.search()`** con qdrant-client ≥ 1.10 — usar `client.query_points()`.
- **No filtrar por un campo de payload sin crear índice** — devuelve 400.
