"""Document text extraction (PDF, DOCX, TXT/MD) and chunking."""
from __future__ import annotations

import io
import logging
import re

logging.getLogger("pypdf").setLevel(logging.ERROR)   # malformed PDFs are reported via ParseError
from dataclasses import dataclass

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}


class ParseError(Exception):
    """Raised when a file cannot be turned into usable text (message is user-safe)."""


@dataclass
class Page:
    number: int | None     # 1-based page number for PDFs, None otherwise
    text: str


@dataclass
class Chunk:
    ord: int
    page: int | None
    text: str


# ------------------------------------------------------------------ extraction
def extension_of(filename: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]+)$", filename or "")
    return m.group(1).lower() if m else ""


def _check_signature(ext: str, data: bytes) -> None:
    """Reject files whose content does not match their extension."""
    if ext == ".pdf" and not data.lstrip()[:5].startswith(b"%PDF-"):
        raise ParseError("file has a .pdf extension but is not a valid PDF")
    if ext == ".docx" and not data[:4] == b"PK\x03\x04":
        raise ParseError("file has a .docx extension but is not a valid Word document")


def extract_pages(filename: str, data: bytes) -> list[Page]:
    ext = extension_of(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise ParseError(f"unsupported file type '{ext or 'none'}'; allowed: "
                         + ", ".join(sorted(SUPPORTED_EXTENSIONS)))
    if not data:
        raise ParseError("file is empty")
    _check_signature(ext, data)
    try:
        if ext == ".pdf":
            pages = _pdf(data)
        elif ext == ".docx":
            pages = _docx(data)
        else:
            pages = [Page(None, _decode_text(data))]
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001 - library-specific errors become one clean message
        raise ParseError(f"could not read {ext} file: {type(e).__name__}") from e

    pages = [Page(p.number, _normalise(p.text)) for p in pages]
    if sum(len(p.text) for p in pages) < 20:
        raise ParseError("no extractable text found (scanned images need OCR, which is not enabled)")
    return pages


def _pdf(data: bytes) -> list[Page]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ParseError("PDF is password-protected")
    return [Page(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]


def _docx(data: bytes) -> list[Page]:
    import docx
    from docx.document import Document as _Doc
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document: _Doc = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    # Walk the body in order so tables stay next to the paragraphs that introduce them
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            lines.append(Paragraph(child, document).text)
        elif child.tag == qn("w:tbl"):
            for row in Table(child, document).rows:
                cells = []
                for cell in row.cells:
                    t = cell.text.strip()
                    if not cells or cells[-1] != t:      # merged cells repeat their text
                        cells.append(t)
                lines.append(" | ".join(cells))
            lines.append("")
    return [Page(None, "\n".join(lines))]


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------------ chunking
def chunk_pages(pages: list[Page], target_chars: int = 900, overlap_chars: int = 150) -> list[Chunk]:
    """Paragraph-aware chunking. Chunks never span PDF pages, so citations stay exact."""
    chunks: list[Chunk] = []
    for page in pages:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page.text) if p.strip()]
        buf = ""
        for para in paragraphs:
            # very long paragraphs are split on line / sentence boundaries
            pieces = [para] if len(para) <= target_chars else _split_long(para, target_chars)
            for piece in pieces:
                if buf and len(buf) + len(piece) + 2 > target_chars:
                    chunks.append(Chunk(len(chunks), page.number, buf))
                    tail = buf[-overlap_chars:]
                    cut = tail.find("\n")
                    buf = (tail[cut + 1:] if cut >= 0 else "") + "\n\n" + piece
                    buf = buf.strip()
                else:
                    buf = f"{buf}\n\n{piece}" if buf else piece
        if buf:
            chunks.append(Chunk(len(chunks), page.number, buf))
    return chunks


def _split_long(text: str, size: int) -> list[str]:
    units = re.split(r"(?<=\n)|(?<=[.!?])\s+", text)
    out, buf = [], ""
    for u in units:
        if not u:
            continue
        if buf and len(buf) + len(u) > size:
            out.append(buf.strip())
            buf = ""
        buf += u if u.endswith("\n") else u + " "
    if buf.strip():
        out.append(buf.strip())
    return out


# ------------------------------------------------------------------ metadata
def guess_title(pages: list[Page], filename: str) -> str:
    for page in pages:
        for line in page.text.splitlines():
            line = line.strip().lstrip("#").strip()
            if 4 <= len(line) <= 120:
                return line
    return filename


def find_effective_date(text: str) -> str | None:
    """'Effective date: 2026-03-01' or 'Effective: 1 March 2026' -> ISO date."""
    m = re.search(r"effective(?: date)?\s*[:\-]?\s*(?:from\s+)?(\d{4})-(\d{2})-(\d{2})", text, re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"effective(?: date)?\s*[:\-]?\s*(?:from\s+)?(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text, re.I)
    if m and m.group(2).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def find_period(text: str) -> str | None:
    """Reporting period declared in a document header, e.g. 'Reporting period: Q2 2026'."""
    m = re.search(r"reporting period\s*[:\-]\s*(Q[1-4])\s*(?:FY)?\s*(\d{4})", text, re.I)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    m = re.search(r"reporting period\s*[:\-]\s*(?:FY|fiscal year)\s*(\d{4})", text, re.I)
    if m:
        return f"FY {m.group(1)}"
    return None
