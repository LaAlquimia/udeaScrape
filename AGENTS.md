# AGENTS.md — Guía para agentes que trabajen en este repo

## Qué es este proyecto

Pipeline que scrappea el Reglamento Estudiantil de Pregrado de la Universidad de Antioquia desde [normativa.udea.edu.co](https://normativa.udea.edu.co), descarga los 28 PDFs resultantes y los vectoriza en [Qdrant Cloud](https://cloud.qdrant.io) con chunking semántico por ideas (marcadores legales españoles: ARTÍCULO, CAPÍTULO, PARÁGRAFO, etc.).

Dos scripts Python:
- `scrape.py` — descarga PDFs + genera Excel + data.json
- `vectorize.py` — extracción de texto → chunking semántico → embeddings → upsert a Qdrant

## Convenciones del repo

- **Python 3.9** en `.venv` (venv existente, ya tiene `requests`, `openpyxl`).
- Los PDFs siguen el patrón de nombre `Acuerdo_<numero>_<fecha>_<docId>.pdf`. No renombrar a mano.
- `data.json` es la fuente de verdad de metadata; si lo regeneras, mantén las claves: `numero`, `docId`, `fechaExpedicion`, `entradaVigencia`, `medioPublicacion`, `resuelve`, `normasRelacionadas`, `archivo`, `tamano_bytes`, `url_pdf`.
- `.env` está en `.gitignore`. **Nunca commitear** `QDRANT_API_KEY` ni `OPENROUTER_API_KEY`.

## Cómo correr el pipeline

### Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Variables de entorno

`cp .env.example .env` y rellenar `QDRANT_URL`, `QDRANT_API_KEY`. Opcional: `OPENROUTER_API_KEY` para embeddings por API (default = fastembed local).

### Validar chunker sin gastar embeddings

```bash
python vectorize.py --dry-run --limit 3
```

Imprime por PDF: nº de chunks, cabecera del primero, label del marcador. Si los labels no son los esperados (`articulo`, `capitulo`, etc.), ajustar regex en `SECTION_PATTERNS`.

> Estado actual verificado: 28 PDFs scrapeados → 2 PDFs de texto extraíble (Acuerdo 1 de 1981 versiones CON/SIN concordancias, ~566 pp combinadas) → **766 chunks** listos para embed. Los otros 26 son PDFs escaneados y requieren `--ocr` después de `brew install tesseract tesseract-lang poppler`.

### Vectorizar todo

```bash
python vectorize.py            # los 28 PDFs (skip los escaneados)
python vectorize.py --limit 5  # solo los 5 primeros (test)
python vectorize.py --ocr     # auto-OCR para PDFs sin texto
python vectorize.py --ocr-only # forzar OCR en todos
```

Re-ejecuciones son idempotentes: `point_id = f"{doc_id}:{chunk_index:04d}"`.

## Puntos de extensión

| Si necesitas... | Edita... |
|---|---|
| Cambiar los marcadores legales reconocidos | `SECTION_PATTERNS` en `vectorize.py` |
| Cambiar el tamaño objetivo / overlap | `TARGET_CHUNK_CHARS`, `OVERLAP_CHARS`, `MIN_CHUNK_CHARS` en `vectorize.py` |
| Cambiar el modelo de embeddings | `DEFAULT_FASTEMBED_MODEL` o `OPENROUTER_EMBED_MODEL` (vía env) |
| Cambiar la colección destino | `--collection` en CLI o `QDRANT_COLLECTION_NAME` en `.env` |
| Añadir OCR para PDFs escaneados | `brew install tesseract tesseract-lang poppler && pip install pytesseract pdf2image`, luego `python vectorize.py --ocr` |
| Cambiar la métrica de distancia Qdrant | `ensure_collection()` — `qm.Distance.COSINE` por defecto |

## Idempotencia y re-corridas

`point_id = f"{doc_id}:{chunk_index:04d}"`. Si vuelves a correr el script:
- Mismo PDF, mismo orden de chunks → mismo `point_id` → **update** in-place (no duplicados).
- Si cambia el orden/conteo de chunks (porque ajustaste regex), algunos `point_id` nuevos aparecen y los viejos huérfanos quedan. Solución: borrar la colección y re-poblar, o usar `client.delete()` antes de re-correr.

Para borrar y recrear:

```python
from qdrant_client import QdrantClient
client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
client.delete_collection("udea_reglamento_pregrado")
```

## Convenciones de chunking — no romper

Los chunks **deben** seguir estos invariantes para que el sistema RAG downstream funcione:

1. **1 chunk ≈ 1 idea legal**: el regex debe partir en marcadores estructurales, NO en caracteres fijos.
2. **El `section_marker` debe ser el encabezado del marcador** (ej. `ARTÍCULO 5.  [articulo]`) para que la UI pueda mostrar de qué norma viene el match.
3. **Overlap con `(...continuación)`** en chunks con índice > 0. Sin esto, las búsquedas que caen en medio de un artículo pierden el contexto del inicio.
4. **Chunks vacíos no se suben a Qdrant** — el script los filtra con `if txt.strip()`.

Si tocas `chunk_by_ideas()`, valida con `--dry-run` antes de re-vectorizar todo.

## Pruebas manuales rápidas

```bash
# Sintaxis del chunker
python -c "import vectorize; print(len(vectorize.SECTION_PATTERNS), 'markers')"

# Dry-run sobre 1 PDF (no toca Qdrant)
python vectorize.py --dry-run --limit 1

# Dry-run sobre todo
python vectorize.py --dry-run
```

## Debugging

| Síntoma | Causa probable | Fix |
|---|---|---|
| Script aborta con "Faltan QDRANT_URL..." | `.env` vacío o sin esas vars | Rellenar `.env` |
| 1 solo chunk por PDF | PDF escaneado, sin texto extraíble | Activar OCR (TODO) |
| Todos los chunks con label `(sin marcador)` | Regex no detecta los marcadores | Inspeccionar texto crudo con `pypdf`, ajustar `SECTION_PATTERNS` |
| `embedder.dim = 0` después de un upsert | Embedder se inicializó con API pero el primer batch fue vacío | Forzar un `--limit 1` antes para crear la colección |
| `collection_exists` falla con error de auth | URL o key mal copiadas | Verificar formato: URL termina en `:6333`, key empieza con `eyJ` o `qkey_` |

## Archivos importantes

- `vectorize.py` — chunker + embedder + upsert. **Aquí es donde pasarás el 90% del tiempo.**
- `scrape.py` — solo tocar si cambia normativa.udea.edu.co.
- `data.json` — no editar a mano.
- `.env` — no commitear.
- `pdfs/` — los originales scrapeados; no modificar.

## Lo que NO hacer

- No subir los PDFs completos a Qdrant como un solo punto cada uno — son docs de 50+ páginas, no se buscan bien.
- No usar `chunk_size` fijo tipo LangChain `CharacterTextSplitter` — destruye la estructura legal.
- No embeddings en inglés para textos en español — usar modelos multilingües (`multilingual-e5`) o modelos entrenados en español.
- No borrar `.venv/` ni `pdfs/` sin confirmar — `.venv` toma tiempo en recrear; `pdfs/` requiere re-scraping.
