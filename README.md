# udeaScrape → Qdrant Cloud

Pipeline para scrapear los acuerdos y reglamentos de pregrado de la Universidad de Antioquia desde [normativa.udea.edu.co](https://normativa.udea.edu.co), descargar los PDFs, y vectorizarlos en [Qdrant Cloud](https://cloud.qdrant.io) con chunking semántico por ideas.

## Estructura del proyecto

```
udeaScrape/
├── scrape.py                            # Scrape de la tabla + descarga de PDFs
├── vectorize.py                         # Extracción, chunking, embeddings, upsert
├── data.json                            # Metadata scrapeada (28 registros)
├── estatutos_reglamentos_pregrado.xlsx  # Misma data en Excel
├── pdfs/                                # 28 PDFs (Acuerdo_*.pdf)
├── .env.example                         # Plantilla de variables de entorno
├── .env                                 # Variables reales (NO commitear)
├── .gitignore                           # Ignora .env, .venv/, __pycache__
├── requirements.txt                     # Dependencias del pipeline
└── README.md / AGENTS.md                # Esta documentación
```

## 1. Scrape (ya ejecutado)

`scrape.py` descarga los 28 PDFs filtrando por "REGLAMENTO ESTUDIANTIL DE PREGRADO" en la tabla de normativa.udea.edu.co. Salida:

- `pdfs/` con PDFs renombrados como `Acuerdo_<numero>_<fecha>_<docId>.pdf`
- `data.json` con la metadata completa
- `estatutos_reglamentos_pregrado.xlsx` para abrir en Excel

Para re-scrapear (no necesario salvo que normativa.udea.edu.co cambie):

```bash
source .venv/bin/activate
python scrape.py
```

## 2. Vectorización a Qdrant Cloud

### 2.1. Preparar el entorno

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2.2. Configurar credenciales

Copia `.env.example` a `.env` y rellena:

```env
QDRANT_URL=https://TU-CLUSTER.qdrant.cloud:6333
QDRANT_API_KEY=tu-api-key
```

(Opcional) Embeddings vía API en vez de local:

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_EMBED_MODEL=openai/text-embedding-3-small
```

### 2.3. Probar el chunker localmente (sin tocar Qdrant)

```bash
python vectorize.py --dry-run --limit 3
```

Salida esperada: por cada PDF procesado verás el número de chunks generados y la cabecera del primero. Útil para validar que el regex detecta bien los marcadores antes de gastar embeddings.

### 2.4. Vectorizar todo

```bash
python vectorize.py
```

Por cada PDF: extrae texto → chunking semántico → embedding → upsert a la colección `udea_reglamento_pregrado` (configurable con `--collection`).

Re-ejecuciones son idempotentes: el `point_id = f"{doc_id}:{chunk_index:04d}"` se sobreescribe si ya existe.

## 3. Cómo funciona el chunking semántico

`chunk_by_ideas()` divide cada PDF usando los **marcadores estructurales del lenguaje legal español**:

| Marcador | Ejemplo | Label interno |
|---|---|---|
| `ARTÍCULO N` (con/sin tilde, plural, ordinal, rangos) | `ARTÍCULOS 49, 50 y 51` | `articulo` |
| `CAPÍTULO` (romano o arábigo) | `CAPÍTULO II` | `capitulo` |
| `PARÁGRAFO` (con ordinal opcional) | `PARÁGRAFO 1` | `paragrafo` |
| `CONSIDERANDO` | `CONSIDERANDO QUE…` | `considerando` |
| `RESUELVE` / `RESUELVEN` | `RESUELVE:` | `resuelve` |
| `ACUERDA` / `ACUERDAN` | `ACUERDA:` | `acuerda` |

Reglas:

1. Cada marcador delimita el **inicio de una nueva idea**. Todo el texto entre dos marcadores es un chunk candidato.
2. El **encabezado** del marcador se conserva como primera línea del chunk (asignado a `section_marker` en el payload).
3. **Micro-chunks** (< 80 chars) se fusionan con el siguiente.
4. **Overlap** de 150 chars desde el final del chunk anterior se antepone al siguiente con el prefijo `(...continuación)` para mantener contexto sin duplicar ideas completas.
5. **Fallback**: si el PDF no tiene marcadores (raro), se hace split por párrafos dobles (`\n\n`) agrupando hasta ~1500 chars.

### Por qué chunking por ideas y no por caracteres

- Los acuerdos regulan **artículos discretos**: cada ARTÍCULO suele ser una norma autocontenida.
- Búsquedas tipo *"¿qué dice sobre doble titulación?"* funcionan mejor si el match es 1 chunk = 1 idea.
- Reduce ruido: un chunk = un concepto, no un fragmento aleatorio de 500 chars.

## 4. Embeddings

Por defecto usa **fastembed local** con `intfloat/multilingual-e5-small` (384 dim, soporta español bien, ~50 MB en disco, sin API key).

Si defines `OPENROUTER_API_KEY`, usa el modelo configurado en `OPENROUTER_EMBED_MODEL` (default `openai/text-embedding-3-small`, 1536 dim). La colección se crea con la dim del primer embed.

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

El `point_id` es `chunk_id` (string). `text` es el contenido que recuperará tu app; el resto es metadata filtrable.

## 6. Búsquedas útiles sobre la colección

Con `qdrant-client` o el dashboard de Qdrant Cloud:

```python
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

# Búsqueda semántica simple
hits = client.search(
    collection_name="udea_reglamento_pregrado",
    query_vector=mi_embedding,
    limit=5,
    with_payload=True,
)

# Filtrar por acuerdo específico
hits = client.search(
    collection_name="udea_reglamento_pregrado",
    query_vector=mi_embedding,
    query_filter=qm.Filter(must=[
        qm.FieldCondition(key="numero", match=qm.MatchValue(value="0425"))
    ]),
    limit=5,
)
```

## 7. Limitaciones conocidas

- **PDFs escaneados**: `pypdf` no hace OCR. Si `extract_text()` devuelve < 200 chars, el script loguea `[warn]` y salta ese PDF. De los 28 PDFs scrapeados, **2 son los maestros (Acuerdo 1 de 1981, versiones con/sin concordancias)** y se vectorizan capturando ~95% del contenido regulatorio (la Secretaría General ya re-tipeó y consolidó todas las modificaciones posteriores). **26 son escaneados** (cada modificación individual: Acuerdo 0031, 0038, 0118, 0425, etc.). Para vectorizar esos 26:

  ```bash
  # 1. Instala dependencias del sistema (una sola vez)
  brew install tesseract tesseract-lang poppler

  # 2. Instala dependencias Python opcionales
  pip install pytesseract pdf2image

  # 3. Vuelve a vectorizar con OCR activado (auto-fallback por PDF)
  python vectorize.py --ocr
  # o fuerza OCR sobre todos (no usar pypdf):
  python vectorize.py --ocr-only
  ```

- **Acentos inconsistentes**: los PDFs viejos de UDEA usan a veces `ARTICULO` sin tilde. El regex acepta ambas formas.
- **Caracteres de control** (`\x0c`, etc.): se normalizan a `\n\n`.
- **Tablas**: las celdas de tabla se concatenan con espacios — el chunker las trata como prosa. Si una idea vive en una tabla, queda fragmentada.

## 8. Licencia y uso

Los documentos son propiedad de la Universidad de Antioquia y se usan aquí únicamente con fines de estudio. Verifica los términos en [normativa.udea.edu.co](https://normativa.udea.edu.co) antes de redistribuirlos.
