#!/usr/bin/env python3
"""
Vectorize UDEA pregrado regulations into Qdrant Cloud.

Pipeline:
  1. Read ./data.json metadata + each ./pdfs/*.pdf
  2. Extract text per page (pypdf)
  3. Chunk semantically by IDEAS using Spanish legal markers:
       ARTÍCULO N, CAPÍTULO, PARÁGRAFO, CONSIDERANDO, RESUELVE, ACUERDA
     Fallback: paragraph-based chunking with overlap if no markers found.
  4. Embed chunks (default: fastembed local multilingual-e5-small,
                  optional: OpenRouter if OPENROUTER_API_KEY is set)
  5. Upsert into Qdrant Cloud collection with rich payload:
       {doc_id, chunk_id, chunk_index, text, source_pdf,
        numero, fecha_expedicion, normas_relacionadas,
        section_marker, char_start, char_end}

Idempotent: re-running updates existing points (point_id = doc_id:chunk_index).

Usage:
  python vectorize.py --dry-run --limit 3        # local preview, no Qdrant
  python vectorize.py --limit 5                 # push first 5 PDFs
  python vectorize.py                          # push all 28 PDFs

Reads QDRANT_URL and QDRANT_API_KEY from .env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from pypdf import PdfReader
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from tenacity import retry, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "pdfs"
JSON_PATH = ROOT / "data.json"

DEFAULT_COLLECTION = os.environ.get("QDRANT_COLLECTION_NAME", "udea_reglamento_pregrado")
# Supported by fastembed out of the box; good multilingual quality including
# Spanish. 384-dim vectors.
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_OPENROUTER_EMBED_MODEL = "openai/text-embedding-3-small"

# --- Spanish legal/regulatory structural markers ---------------------------
# Each marker is captured as a regex; matched chunks keep the marker as header.
SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ARTÍCULO(S) with optional accent, ordinal/decimal, optional range
    ("articulo", re.compile(
        r"(?im)^[ \t]*ART[ÍI]CULOS?\s+"
        r"(?:\d+(?:[º°oa\.\)]?)|\d+\s*(?:y|,)\s*\d+|primero|segundo|tercero|cuarto|quinto)"
        r"[^\n]{0,80}\n?", re.UNICODE)),
    ("capitulo", re.compile(
        r"(?im)^[ \t]*CAP[ÍI]TULO\s+[IVXLCDM]+\b[^\n]{0,80}\n?")),
    ("paragrafo", re.compile(
        r"(?im)^[ \t]*PAR[ÁA]GRAFO\s*(?:\d+|[único]?)?[^\n]{0,80}\n?")),
    ("considerando", re.compile(
        r"(?im)^[ \t]*CONSIDERANDOS?\b[^\n]{0,80}\n?")),
    ("resuelve", re.compile(
        r"(?im)^[ \t]*RESUELV[EN]?\b[^\n]{0,80}\n?")),
    ("acuerda", re.compile(
        r"(?im)^[ \t]*ACUERD[AE]N?\b[^\n]{0,80}\n?")),
]

# Combine into one alternation for fast scanning.
# We keep the ordered list above to attach the right section_marker label.
MIN_CHUNK_CHARS = 80         # merge micro-chunks smaller than this with next
TARGET_CHUNK_CHARS = 1500    # soft target; paragraph fallback splits near this
OVERLAP_CHARS = 150          # carried over from previous chunk for context


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Chunk:
    chunk_index: int
    text: str
    section_marker: str
    char_start: int
    char_end: int


@dataclass
class DocRecord:
    doc_id: str
    numero: str
    fecha_expedicion: str
    normas_relacionadas: str
    archivo: str


# ---------------------------------------------------------------------------
# Metadata loader
# ---------------------------------------------------------------------------
def load_records() -> dict[str, DocRecord]:
    """Return {doc_id: DocRecord} from data.json."""
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    out: dict[str, DocRecord] = {}
    for row in data:
        out[row["docId"]] = DocRecord(
            doc_id=row["docId"],
            numero=row["numero"],
            fecha_expedicion=row["fechaExpedicion"].replace("/", "-"),
            normas_relacionadas=row.get("normasRelacionadas", "") or "",
            archivo=row["archivo"],
        )
    return out


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_path: Path) -> str:
    """Concatenate page texts with form-feeds; never raises."""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        print(f"  [warn] cannot open {pdf_path.name}: {e}", file=sys.stderr)
        return ""
    pages_text: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception as e:
            print(f"  [warn] page {i} extract failed for {pdf_path.name}: {e}",
                  file=sys.stderr)
            t = ""
        pages_text.append(t)
    return "\n".join(pages_text).strip()


def extract_pdf_text_ocr(pdf_path: Path, dpi: int = 200) -> str:
    """OCR fallback for scanned PDFs. Requires `pytesseract` + `pdf2image`
    (which itself needs `poppler` on PATH). Falls back to a clear error
    message if any dependency is missing."""
    try:
        import pytesseract                                   # type: ignore[import-unresolved]  # noqa: F401
        from pdf2image import convert_from_path              # type: ignore[import-unresolved]
    except ImportError as e:
        raise RuntimeError(
            f"OCR requested but dependency missing: {e}. "
            "Install with: pip install pytesseract pdf2image && "
            "brew install tesseract tesseract-lang poppler"
        ) from e
    try:
        images = convert_from_path(str(pdf_path), dpi=dpi)
    except Exception as e:
        raise RuntimeError(
            f"pdf2image failed (¿poppler instalado?): {e}"
        ) from e
    import pytesseract  # type: ignore[import-unresolved]  # re-import after assert
    pages_text: list[str] = []
    for i, img in enumerate(images):
        try:
            t = pytesseract.image_to_string(img, lang="spa")
        except Exception as e:
            print(f"  [warn] OCR page {i} failed for {pdf_path.name}: {e}",
                  file=sys.stderr)
            t = ""
        pages_text.append(t)
    return "\n".join(pages_text).strip()


# ---------------------------------------------------------------------------
# Semantic chunking
# ---------------------------------------------------------------------------
def _find_offsets(text: str) -> list[tuple[int, str, re.Match[str]]]:
    """Return [(char_offset, marker_label, match), ...] sorted by offset."""
    hits: list[tuple[int, str, re.Match[str]]] = []
    for label, pat in SECTION_PATTERNS:
        for m in pat.finditer(text):
            hits.append((m.start(), label, m))
    hits.sort(key=lambda x: x[0])
    # Drop near-duplicates (same offset, different patterns) — keep first label.
    deduped: list[tuple[int, str, re.Match[str]]] = []
    last_off = -1
    for off, lbl, m in hits:
        if off - last_off > 2:
            deduped.append((off, lbl, m))
            last_off = off
    return deduped


def _paragraph_split(text: str, target: int, overlap: int) -> list[tuple[str, int, int]]:
    """Fallback: split by double-newline, grouping paragraphs up to target chars."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[tuple[str, int, int]] = []
    buf = ""
    cursor = 0
    for p in paras:
        if buf and len(buf) + len(p) + 2 > target:
            out.append((buf, cursor, cursor + len(buf)))
            cursor += max(0, len(buf) - overlap)
            buf = text[cursor:cursor + overlap] if overlap else ""
        buf = (buf + "\n\n" + p).strip() if buf else p
    if buf:
        out.append((buf, cursor, cursor + len(buf)))
    return out


def chunk_by_ideas(text: str) -> list[Chunk]:
    """Split text into chunks grouped by structural markers + idea continuity.

    Strategy:
      - locate all marker offsets
      - everything between two markers is one chunk
      - the header text of the marker stays at the top of its chunk
      - chunks below MIN_CHUNK_CHARS are merged into the next one
      - each chunk carries overlap_chars from the previous chunk's tail
      - if zero markers found: fall back to paragraph chunking
    """
    text = re.sub(r"[ \t]+\n", "\n", text)         # collapse trailing spaces
    text = re.sub(r"\n{3,}", "\n\n", text)          # normalize blank lines
    text = text.strip()
    if not text:
        return []

    offsets = _find_offsets(text)

    spans: list[tuple[str, int, int, str]] = []
    if not offsets:
        # Fallback: no structural markers detected.
        for i, (txt, cs, ce) in enumerate(
            _paragraph_split(text, TARGET_CHUNK_CHARS, OVERLAP_CHARS)
        ):
            if not txt.strip():
                continue
            spans.append((txt, cs, ce, "(sin marcador)"))
    else:
        for i, (off, lbl, m) in enumerate(offsets):
            end = offsets[i + 1][0] if i + 1 < len(offsets) else len(text)
            body = text[off:end].strip()
            header_line = m.group(0).strip().split("\n", 1)[0]
            spans.append((body, off, end, f"{header_line}  [{lbl}]"))

    if not spans or not any(s[0].strip() for s in spans):
        return []

    # Merge micro-chunks into the next one.
    merged: list[tuple[str, int, int, str]] = []
    for body, cs, ce, lbl in spans:
        if merged and len(merged[-1][0]) < MIN_CHUNK_CHARS:
            prev_body, prev_cs, _, prev_lbl = merged[-1]
            merged[-1] = (
                f"{prev_body}\n\n{body}",
                prev_cs,
                ce,
                prev_lbl,
            )
        else:
            merged.append((body, cs, ce, lbl))

    # Build Chunk objects, prepend overlap from previous tail for context.
    chunks: list[Chunk] = []
    for i, (body, cs, ce, lbl) in enumerate(merged):
        if i == 0 or not body.strip():
            text_chunk = body
        else:
            tail = chunks[-1].text[-OVERLAP_CHARS:]
            # find last sentence boundary in tail
            cut = max(tail.rfind(". "), tail.rfind(".\n"))
            if cut > 30:
                tail = tail[cut + 2:]
            text_chunk = f"(...continuación)\n{tail}\n\n{body}".strip()
        chunks.append(Chunk(
            chunk_index=len(chunks),
            text=text_chunk,
            section_marker=lbl,
            char_start=cs,
            char_end=ce,
        ))
    return chunks


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
class Embedder:
    """Strategy: OpenRouter if OPENROUTER_API_KEY is set, else fastembed local."""

    def __init__(self) -> None:
        self.kind: str
        self.dim: int = 0
        self._model_name: str

        if os.environ.get("OPENROUTER_API_KEY"):
            import httpx  # already installed transitively
            self._client: httpx.Client | None = httpx.Client(timeout=60)
            self._model = None
            self._model_name = os.environ.get(
                "OPENROUTER_EMBED_MODEL", DEFAULT_OPENROUTER_EMBED_MODEL)
            self.kind = f"openrouter:{self._model_name}"
            # text-embedding-3-small is 1536; 3-large is 3072. Probe on first call.
            self.dim = 1536 if "3-small" in self._model_name else 3072
        else:
            from fastembed import TextEmbedding
            self._client = None
            self._model: TextEmbedding | None = TextEmbedding(
                model_name=DEFAULT_FASTEMBED_MODEL
                if not os.environ.get("FASTEMBED_MODEL")
                else os.environ["FASTEMBED_MODEL"]
            )
            self._model_name = self._model.model_name  # type: ignore[union-attr]
            self.kind = f"fastembed:{self._model_name}"
            # fastembed models expose .embedding_size via first embed; we probe
            # lazily so dim stays 0 until the first call.

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._client is not None:
            return self._embed_openrouter(texts)
        return self._embed_fastembed(texts)

    def _embed_fastembed(self, texts: list[str]) -> list[list[float]]:
        # multilingual-e5 models expect "passage: " prefix for documents
        assert self._model is not None, "fastembed model not initialized"
        prefix = "passage: " if "e5" in self._model_name.lower() else ""
        vecs = list(self._model.embed([prefix + t for t in texts]))
        if self.dim == 0 and vecs:
            self.dim = len(vecs[0])
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=15))
    def _embed_openrouter(self, texts: list[str]) -> list[list[float]]:
        # OpenRouter exposes OpenAI-compatible /embeddings
        assert self._client is not None, "OpenRouter client not initialized"
        r = self._client.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "Content-Type": "application/json",
            },
            json={"model": self._model_name, "input": texts},
        )
        r.raise_for_status()
        data = r.json()["data"]
        return [item["embedding"] for item in data]


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------
def get_qdrant() -> QdrantClient:
    url = os.environ.get("QDRANT_URL", "").strip()
    key = os.environ.get("QDRANT_API_KEY", "").strip()
    if not url or not key:
        raise SystemExit(
            "Faltan QDRANT_URL y/o QDRANT_API_KEY en .env. "
            "Cópialos desde https://cloud.qdrant.io y vuelve a correr."
        )
    return QdrantClient(
        url=url,
        api_key=key,
        timeout=300,           # seconds; Cloud free tier can be slow
        check_compatibility=False,
    )


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    if client.collection_exists(name):
        return
    client.create_collection(
        collection_name=name,
        vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        optimizers_config=qm.OptimizersConfigDiff(default_segment_number=2),
    )


def make_point_id(doc_id: str, chunk_index: int) -> str:
    return f"{doc_id}:{chunk_index:04d}"


def upsert_batched(
    client: QdrantClient,
    collection: str,
    points: list[qm.PointStruct],
    batch_size: int = 64,
) -> int:
    """Upsert in small batches with retry on transient failures.

    Qdrant Cloud's free tier can take 30-90s to accept a batch with many
    points (especially the first batch, when the collection is being
    initialized). `wait=False` returns as soon as the server enqueues the
    batch; indexing happens in the background.
    """
    sent = 0
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        @retry(stop=stop_after_attempt(5),
               wait=wait_exponential(multiplier=2, min=4, max=60),
               retry_error_callback=lambda state: (
                   print(f"  [retry] batch starting at {i}, attempt "
                         f"{state.attempt_number}", file=sys.stderr)
                   or None
               ))
        def _send(b=batch):
            client.upsert(collection_name=collection, points=b, wait=False)
        _send()
        sent += len(batch)
        if (i // batch_size) % 5 == 0:
            print(f"    upserted {sent}/{len(points)}", file=sys.stderr)
    return sent


def build_points(
    chunks: list[Chunk],
    vectors: list[list[float]],
    rec: DocRecord,
) -> Iterable[qm.PointStruct]:
    assert len(chunks) == len(vectors)
    for ch, vec in zip(chunks, vectors):
        payload = {
            "doc_id": rec.doc_id,
            "chunk_id": make_point_id(rec.doc_id, ch.chunk_index),
            "chunk_index": ch.chunk_index,
            "text": ch.text,
            "section_marker": ch.section_marker,
            "source_pdf": rec.archivo,
            "numero": rec.numero,
            "fecha_expedicion": rec.fecha_expedicion,
            "normas_relacionadas": rec.normas_relacionadas,
            "char_start": ch.char_start,
            "char_end": ch.char_end,
        }
        yield qm.PointStruct(id=payload["chunk_id"], vector=vec, payload=payload)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def process_pdf(
    pdf_path: Path,
    rec: DocRecord,
    embedder: Embedder,
    client: QdrantClient | None,
    collection: str,
    dry_run: bool,
    use_ocr: bool = False,
    ocr_only: bool = False,
) -> dict:
    t0 = time.time()
    text = extract_pdf_text(pdf_path)
    text_source = "pypdf"
    if (ocr_only or (use_ocr and len(text) < 200)):
        if len(text) < 200:
            print(f"  [ocr] {pdf_path.name}: pypdf devolvió {len(text)} chars, "
                  f"reintentando con OCR…", file=sys.stderr)
        text = extract_pdf_text_ocr(pdf_path)
        text_source = "ocr"
    extract_secs = time.time() - t0

    if len(text) < 200:
        return {
            "pdf": pdf_path.name,
            "skipped": True,
            "source": text_source,
            "reason": f"text extracted = {len(text)} chars (¿escaneado?)",
        }

    chunks = chunk_by_ideas(text)
    if not chunks:
        return {"pdf": pdf_path.name, "skipped": True, "reason": "no chunks"}

    # Show a small sample
    sample = chunks[0].text[:240].replace("\n", " ")
    print(f"  {pdf_path.name}: {len(text):,} chars -> {len(chunks)} chunks "
          f"| first: «{sample}…»", file=sys.stderr)

    if dry_run or client is None:
        return {
            "pdf": pdf_path.name,
            "chunks": len(chunks),
            "chars": len(text),
            "extract_secs": round(extract_secs, 2),
            "sample_section": chunks[0].section_marker,
            "sample_chunk_idx": chunks[0].chunk_index,
        }

    t1 = time.time()
    vectors = embedder.embed([c.text for c in chunks])
    embed_secs = time.time() - t1

    if embedder.dim == 0 and vectors:
        embedder.dim = len(vectors[0])
    ensure_collection(client, collection, embedder.dim)

    t2 = time.time()
    points = list(build_points(chunks, vectors, rec))
    sent = upsert_batched(client, collection, points, batch_size=64)
    upsert_secs = time.time() - t2

    return {
        "pdf": pdf_path.name,
        "chunks": len(chunks),
        "vectors": len(vectors),
        "upserted": sent,
        "extract_secs": round(extract_secs, 2),
        "embed_secs": round(embed_secs, 2),
        "upsert_secs": round(upsert_secs, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Vectorize UDEA PDFs into Qdrant Cloud.")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help=f"Nombre de la colección (default: {DEFAULT_COLLECTION})")
    ap.add_argument("--limit", type=int, default=0,
                    help="Procesar solo los primeros N PDFs (0=todos)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo chunking + embedding local, sin tocar Qdrant")
    ap.add_argument("--ocr", action="store_true",
                    help="Usar OCR (tesseract) si pypdf extrae <200 chars")
    ap.add_argument("--ocr-only", action="store_true",
                    help="Saltarse pypdf y usar OCR para todos los PDFs")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    records = load_records()

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[:args.limit]
    print(f"[info] {len(pdfs)} PDFs a procesar | colección={args.collection} | "
          f"dry_run={args.dry_run}", file=sys.stderr)

    client: QdrantClient | None = None
    embedder = Embedder()
    print(f"[info] embedder = {embedder.kind}", file=sys.stderr)

    if not args.dry_run:
        client = get_qdrant()
        # Ensure collection with placeholder dim; will be re-created if dim differs
        # on first successful embed.
        try:
            info = client.get_collection(args.collection)
            existing = info.config.params.vectors.size  # type: ignore[attr-defined]
            print(f"[info] colección existente, dim={existing}", file=sys.stderr)
            embedder.dim = existing
        except Exception:
            pass  # will be created after first embed

    summary: list[dict] = []
    for pdf in pdfs:
        rec = records.get(pdf.stem.split("_")[-1])
        if rec is None:
            # Fallback: scan data.json for matching filename
            for r in records.values():
                if r.archivo == pdf.name:
                    rec = r
                    break
        if rec is None:
            print(f"  [warn] sin metadata para {pdf.name}, saltando", file=sys.stderr)
            continue
        try:
            res = process_pdf(pdf, rec, embedder, client, args.collection,
                              args.dry_run, use_ocr=args.ocr,
                              ocr_only=args.ocr_only)
        except Exception as e:
            res = {"pdf": pdf.name, "error": str(e)}
            print(f"  [error] {pdf.name}: {e}", file=sys.stderr)
        summary.append(res)

    print("\n=== RESUMEN ===")
    for r in summary:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
