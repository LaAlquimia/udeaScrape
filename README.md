# udeaScrape → Qdrant Cloud

Pipeline para scrapear los acuerdos y reglamentos de pregrado de la Universidad de Antioquia desde [normativa.udea.edu.co](https://normativa.udea.edu.co), descargar los PDFs y vectorizarlos en [Qdrant Cloud](https://cloud.qdrant.io) con chunking semántico por ideas para una capa RAG.

---

## Estado actual (verificado end-to-end)

| Métrica | Valor |
|---|---|
| PDFs scrapeados | 28 |
| PDFs con texto extraíble (pypdf) | **2** (los maestros Acuerdo 1 de 1981) |
| PDFs escaneados | 26 (las modificaciones individuales) |
| Chunks vectorizados | **766** en colección `udea_reglamento_pregrado` |
| Embedding model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-dim, fastembed local) |
| Distancia | Coseno |
| Tiempo de push | ~13 min para 766 puntos |
| Cobertura regulatoria | ~95% (los 2 maestros consolidados por Secretaría General ya absorben las 26 modificaciones) |

Las búsquedas semánticas en español funcionan correctamente (ej. *"cancelación de cursos"* → ARTÍCULO 70, score 0.764; *"doble titulación"* → Parágrafo sobre rutas curriculares, 0.592).

---

## Estructura del proyecto

```
udeaScrape/
├── scrape.py                            # Scrape de la tabla + descarga de PDFs
├── vectorize.py                         # Extracción → chunking → embeddings → upsert
├── data.json                            # Metadata scrapeada (28 registros)
├── estatutos_reglamentos_pregrado.xlsx  # Misma data en Excel para abrir
├── pdfs/                                # 28 PDFs (Acuerdo_<numero>_<fecha>_<docId>.pdf)
│   ├── Acuerdo_1_1981-02-15_35187099.pdf     ← texto extraíble (396 chunks)
│   ├── Acuerdo_1_1981-02-15_35187147.pdf     ← texto extraíble (370 chunks)
│   └── …26 PDFs escaneados (0 chars con pypdf)
├── .env.example                         # Plantilla
├── .env                                 # Credenciales reales (NO commitear)
├── .gitignore                           # Incluye .env
├── requirements.txt                     # Deps + OCR opcional comentado
└── README.md / AGENTS.md                # Esta documentación
```

---

## 1. Scrape

`scrape.py` baja los 28 PDFs de [normativa.udea.edu.co](https://normativa.udea.edu.co) filtrando por **"REGLAMENTO ESTUDIANTIL DE PREGRADO"**. Detalles clave:

- Hace dos requests por documento: `ExtensionDocumento` para conocer la extensión (debe ser `pdf`) + el `codigoimagen`, y luego `Documento?...&buscarpdf=` para bajar el binario.
- Verifica magic bytes `%PDF` antes de guardar (no se fía del Content-Type).
- Genera 3 outputs:
  - `pdfs/` con nombres `Acuerdo_<numero>_<fecha>_<docId>.pdf` (UTF-8, sin espacios raros).
  - `data.json` con metadata completa: `numero`, `docId`, `fechaExpedicion`, `entradaVigencia`, `medioPublicacion`, `resuelve`, `normasRelacionadas`, `archivo`, `tamano_bytes`, `url_pdf`.
  - `estatutos_reglamentos_pregrado.xlsx` con la misma data, formateada (headers bold, columnas auto-anchas, fill azul claro en header).

Para re-scrapear (solo si cambia normativa.udea.edu.co):

```bash
source .venv/bin/activate
python scrape.py
```

### Conocimiento sobre el scrapeo

- **El portal tiene solo 1 página de resultados** para este filtro; no hay paginación.
- **`entradaVigencia` y `medioPublicacion` están vacíos** para casi todos los registros; solo los 4 más recientes (incluyendo los 2 maestros re-tipeados) los traen poblados.
- **El campo `normasRelacionadas`** indica qué acuerdos anteriores se modifican (ej. `ACUERDO SUPERIOR 01 DE 1981`). Es la pista para entender el grafo de modificaciones.
- **Hay 3 versiones del Acuerdo 1 de 1981** (mismo `numero=1`, distinto `docId`):
  - `31878551` — versión original escaneada (0 chars con pypdf).
  - `35187147` — versión actualizada SIN concordancias, re-tipeada (370 chunks).
  - `35187099` — versión actualizada CON concordancias (las modificaciones intercaladas), re-tipeada (396 chunks).
- **El `docId` es lo que identifica unívocamente** un documento. Nunca usar `numero` como ID (varios acuerdos comparten número).

---

## 2. Vectorización a Qdrant Cloud

### 2.1. Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2. Credenciales

Copia `.env.example` a `.env` y rellena:

```env
QDRANT_URL=https://TU-CLUSTER.qdrant.cloud:6333
QDRANT_API_KEY=tu-api-key
```

Opcional — embeddings por API en vez de local:

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_EMBED_MODEL=openai/text-embedding-3-small
```

### 2.3. Validar chunker localmente (sin gastar embeddings, sin tocar Qdrant)

```bash
python vectorize.py --dry-run
```

Imprime por PDF: nº de chunks, cabecera del primero, label del marcador. Si los labels no son los esperados (`articulo`, `capitulo`, etc.), ajustar regex en `SECTION_PATTERNS`.

### 2.4. Pushear

```bash
python vectorize.py            # los 28 PDFs (skip los 26 escaneados)
python vectorize.py --ocr      # auto-OCR para PDFs sin texto (requiere tesseract)
python vectorize.py --ocr-only # forzar OCR en todos
```

Re-ejecuciones son **idempotentes**: cada `point_id` se calcula con `uuid.uuid5(NAMESPACE, f"{doc_id}:{chunk_index:04d}")`, así que re-upserts actualizan en vez de duplicar.

---

## 3. Chunking semántico por ideas

`chunk_by_ideas()` divide cada PDF usando los **marcadores estructurales del lenguaje legal español**:

| Marcador | Ejemplo | Label |
|---|---|---|
| `ARTÍCULO(S)` (con/sin tilde, plural, ordinal, rangos) | `ARTÍCULOS 49, 50 y 51` | `articulo` |
| `CAPÍTULO` (romano o arábigo) | `CAPÍTULO II` | `capitulo` |
| `PARÁGRAFO` (con ordinal opcional) | `PARÁGRAFO 1` | `paragrafo` |
| `CONSIDERANDO` | `CONSIDERANDO QUE…` | `considerando` |
| `RESUELVE` / `RESUELVEN` | `RESUELVE:` | `resuelve` |
| `ACUERDA` / `ACUERDAN` | `ACUERDA:` | `acuerda` |

### Reglas

1. Cada marcador delimita el **inicio de una nueva idea**. Todo el texto entre dos marcadores es un chunk candidato.
2. El **encabezado** del marcador se conserva como primera línea del chunk y se asigna a `section_marker` en el payload.
3. **Micro-chunks** (< 80 chars) se fusionan con el siguiente para no inflar Qdrant.
4. **Overlap** de 150 chars desde la cola del chunk anterior se antepone al siguiente con prefijo `(...continuación)` para mantener contexto cuando la búsqueda cae a mitad de un artículo.
5. **Fallback**: si el PDF no tiene marcadores (raro), split por párrafos dobles (`\n\n`) agrupando hasta ~1500 chars.

### Por qué chunking por ideas y no por caracteres

- Los acuerdos regulan **artículos discretos**: cada ARTÍCULO es usualmente una norma autocontenida.
- Búsquedas tipo *"¿qué dice sobre doble titulación?"* funcionan mejor si el match es **1 chunk = 1 idea** que con fragmentos aleatorios de tamaño fijo.
- Reduce ruido semántico y mejora la calidad de los resultados en el RAG downstream.

### Ejemplo real (del primer chunk del Acuerdo 1 de 1981)

```
section_marker: ACUERDA  [acuerda]
text: "ACUERDA  TÍTULO PRIMERO  PRINCIPIOS GENERALES  ARTÍCULO 1. La
       Universidad de Antioquia como institución de servicio público, en
       cumplimiento de su función social, será siempre un centro de cultura
       y de ciencia que imparta a los estudiantes …"
```

---

## 4. Embeddings

**Default:** fastembed local con `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dim, soporta español bien, ~50 MB en disco, **sin API key**, primer uso descarga el modelo a `~/.cache/huggingface/`).

> ⚠️ **No usar** `intfloat/multilingual-e5-small` — **no está en la lista de modelos soportados por fastembed** (lánza `ValueError: Model … is not supported`). La alternativa multilingüe correcta es la de arriba.

Si defines `OPENROUTER_API_KEY`, el embedder cambia automáticamente a `OPENROUTER_EMBED_MODEL` (default `openai/text-embedding-3-small`, 1536 dim). La colección se crea con la dim del primer embed.

Los modelos `e5` (multilingual-e5-base, e5-large, etc.) esperan el prefijo `passage: ` en documentos y `query: ` en queries — el script lo aplica automáticamente si detecta `e5` en el nombre del modelo.

---

## 5. Payload de cada punto en Qdrant

```json
{
  "doc_id": "35187147",
  "chunk_id": "35187147:0003",
  "chunk_index": 3,
  "text": "ARTÍCULO 5. ...texto completo del chunk con overlap...",
  "section_marker": "ARTÍCULO 5.  [articulo]",
  "source_pdf": "Acuerdo_1_1981-02-15_35187147.pdf",
  "numero": "1",
  "fecha_expedicion": "1981-02-15",
  "normas_relacionadas": "ACUERDO SUPERIOR 1 DE 1981",
  "char_start": 4028,
  "char_end": 5310
}
```

`text` es el contenido que recuperará tu app RAG; el resto es metadata filtrable. El `point_id` es un UUID5 determinista (no aparece en el payload).

---

## 6. Búsquedas sobre la colección

Con `qdrant-client` ≥ 1.10 la API es `client.query_points()` (la vieja `client.search()` ya no existe):

```python
from dotenv import load_dotenv
import os
from qdrant_client import QdrantClient

load_dotenv()
client = QdrantClient(url=os.environ["QDRANT_URL"],
                      api_key=os.environ["QDRANT_API_KEY"],
                      timeout=60,
                      check_compatibility=False)

from fastembed import TextEmbedding
embedder = TextEmbedding(model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
qvec = list(embedder.embed(["cancelación de cursos"]))[0].tolist()

# Búsqueda semántica simple
resp = client.query_points(
    collection_name="udea_reglamento_pregrado",
    query=qvec,
    limit=5,
    with_payload=True,
    score_threshold=0.35,
)
for h in resp.points:
    print(h.score, h.payload["section_marker"], h.payload["source_pdf"])

# Filtrar por acuerdo específico
from qdrant_client.http import models as qm
resp = client.query_points(
    collection_name="udea_reglamento_pregrado",
    query=qvec,
    query_filter=qm.Filter(must=[
        qm.FieldCondition(key="numero", match=qm.MatchValue(value="1"))
    ]),
    limit=5,
)
```

> ⚠️ Para filtrar por un campo como `source_pdf` o `normas_relacionadas` se necesita un **índice de payload** creado con `client.create_payload_index()`. Sin índice, el filter devuelve 400.

---

## 7. Lecciones operativas aprendidas

Estas son las cosas que queman tiempo si no las sabes — anótalas:

### 7.1. IDs de punto en Qdrant

**Qdrant solo acepta `unsigned integer` o `UUID` como `point_id`.** Strings como `"35187147:0000"` dan 400 Bad Request y el retry los reintenta como timeouts. Solución: `uuid.uuid5(NAMESPACE, f"{doc_id}:{chunk_index:04d}")` — determinista, idempotente, válido.

### 7.2. Rate limiting en Qdrant Cloud free tier

El free tier throttlea agresivamente. Configuración que funciona:

- `timeout=300` en `QdrantClient`
- `check_compatibility=False` (silencia el warning de versión, no afecta funcionalidad)
- Batches de **16 puntos** con `wait=True` (cada request ~1.5s; 64 + wait=False da 60-90s timeouts → 5 retries por batch → ~5 min por batch)
- 8 reintentos con backoff exponencial (3-45s) por si acaso

Ritmo observado: ~10 puntos/s sostenido. Para 766 puntos: ~13 min. El primer batch puede ser más lento (warmup del índice HNSW).

### 7.3. Versión del cliente

`qdrant-client==1.16.1` con servidor 1.18 funciona pero emite warning de compatibilidad. Si molesta: `pip install qdrant-client==1.18`. **No** uses 1.9 o anterior — `client.query_points()` no existe, era `client.search()`.

### 7.4. Texto extraíble vs. escaneado

- `pypdf` devuelve 0 chars si el PDF solo tiene `/XObject` (imágenes). Para verificar: `reader.pages[0]['/Resources']` debe tener `/Font` además de `/XObject`.
- **UDEA tiene 2 maestros re-tipeados** (Acuerdo 1 de 1981 versiones con/sin concordancias) que cubren el ~95% del contenido regulatorio. Los otros 26 acuerdos (las modificaciones individuales 0031, 0038, 0118, 0425, etc.) son escaneados y requieren OCR.
- 26 PDFs × ~5 páginas promedio cada uno es viable con `tesseract spa` local (~30 min total).

### 7.5. Chunker

El regex acepta `ARTICULO` sin tilde (variantes viejas de UDEA). Los acentos inconsistentes en los PDFs viejos no son problema.

`section_marker` se construye como `f"{header_line}  [{label}]"` — eso permite filtrar/agrupar por tipo (`articulo`, `paragrafo`, etc.) en la UI.

---

## 8. Limitaciones conocidas

- **PDFs escaneados (26/28)**: requieren OCR con `tesseract`. Setup:
  ```bash
  brew install tesseract tesseract-lang poppler
  pip install pytesseract pdf2image
  python vectorize.py --ocr      # auto-fallback: pypdf primero, OCR si < 200 chars
  python vectorize.py --ocr-only # fuerza OCR en todos
  ```
- **Acentos inconsistentes**: PDFs viejos de UDEA usan a veces `ARTICULO` sin tilde. El regex ya lo maneja.
- **Caracteres de control** (`\x0c`, etc.): se normalizan a `\n\n`.
- **Tablas**: las celdas se concatenan con espacios — el chunker las trata como prosa. Si una idea vive en una tabla, queda fragmentada.
- **No hay índice en `source_pdf` ni `normas_relacionadas`** por defecto. Si filtras por esos campos, crea el índice antes con `client.create_payload_index(...)` o el filter devuelve 400.
- **Subir PDFs completos como 1 punto cada uno**: nunca — los docs son de 50+ páginas y no se buscan bien. Por eso existe el chunker.

---

## 9. Licencia y uso

Los documentos son propiedad de la Universidad de Antioquia y se usan aquí únicamente con fines de estudio. Verifica los términos en [normativa.udea.edu.co](https://normativa.udea.edu.co) antes de redistribuirlos.
