"""
Programmatic document fixtures for the upload-hardening tests (Task 5).

Nothing here is a stored binary. Every PDF / DOCX / ZIP is generated at test time, because

  * scripts/make_source_dist.py refuses to package .pdf / .docx files, so none may live in the tree;
  * the tests then need nothing beyond python-docx, which the application already depends on
    (the PDFs are hand-assembled, so no reportlab / pypdf dev dependency is introduced).
"""
import io
import zipfile
from typing import List, Optional, Sequence

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")        # legacy .doc AND password-protected .docx containers

RESUME_LINES: List[str] = [
    "Jane Smith",
    "jane.smith@example.com | +91 98765 43210",
    "SUMMARY",
    "Experienced software engineer with 6 years of experience building scalable web applications.",
    "EXPERIENCE",
    "Senior Python Engineer - Acme Corp (2020 - Present)",
    "Built REST APIs using FastAPI and Django. Managed PostgreSQL and MongoDB databases.",
    "Deployed microservices to AWS using Docker and Kubernetes.",
    "EDUCATION",
    "B.E. Computer Science - Anna University (2018)",
    "SKILLS",
    "Python, Java, SQL, Docker, Git, AWS, FastAPI, Django, React",
]


# ── PDF ──────────────────────────────────────────────────────────────────────

def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: Optional[Sequence[Sequence[str]]] = None, *, encrypted: bool = False) -> bytes:
    """
    A small, valid, text-bearing PDF (Helvetica), assembled by hand.

    pages     -- one list of text lines per page; default is a single résumé-like page.
                 A page with no lines has an empty content stream (extractable text: none),
                 which is what an image-only / scanned page looks like to a text extractor.
    encrypted -- adds a Standard-security-handler /Encrypt dictionary whose user password cannot
                 be the empty string, so opening the file demands a password (like a real
                 password-protected résumé).
    """
    if pages is None:
        pages = [RESUME_LINES]
    n_pages = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        content_no = 4 + 2 * i + 1
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_no} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        ops, y = [], 800
        for line in lines:
            ops.append(f"BT /F1 11 Tf 40 {y} Td ({_pdf_escape(line)}) Tj ET")
            y = y - 16 if y > 40 else 800
        stream = "\n".join(ops).encode("latin-1", "replace")
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    encrypt_entry = ""
    if encrypted:
        objs.append(b"<< /Filter /Standard /V 1 /R 2 /O <" + b"00" * 32 + b"> /U <" + b"11" * 32 + b"> /P -4 >>")
        encrypt_entry = (f" /Encrypt {len(objs)} 0 R "
                         "/ID [<0123456789abcdef0123456789abcdef> <0123456789abcdef0123456789abcdef>]")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R{encrypt_entry} >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return out.getvalue()


def make_blank_pdf(pages: int = 1) -> bytes:
    """A PDF whose pages carry no text at all (what a scanned résumé looks like to pdfplumber)."""
    return make_pdf([[] for _ in range(pages)])


# ── DOCX ─────────────────────────────────────────────────────────────────────

def make_docx(lines: Optional[Sequence[str]] = None) -> bytes:
    from docx import Document

    document = Document()
    for line in (RESUME_LINES if lines is None else lines):
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx_bomb(uncompressed_mb: int = 8) -> bytes:
    """
    A structurally valid .docx whose word/document.xml inflates to `uncompressed_mb` MB while the
    file itself stays tiny (a decompression bomb). The first MB is real text so a naive parser
    would happily produce output.
    """
    head = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W_NS}"><w:body>'
    chunk = ("<w:p><w:r><w:t>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</w:t></w:r></w:p>" * 4000).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        with archive.open("word/document.xml", "w", force_zip64=True) as part:
            part.write(head.encode())
            for _ in range(max(1, (uncompressed_mb * 1024 * 1024) // len(chunk))):
                part.write(chunk)
            part.write(b"</w:body></w:document>")
    return out.getvalue()


def make_zip(entries: dict) -> bytes:
    """A plain ZIP archive (used for 'valid zip that is not a .docx' cases)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


def make_docx_many_entries(count: int) -> bytes:
    """A .docx-shaped archive padded with `count` extra tiny entries."""
    entries = {"[Content_Types].xml": _CONTENT_TYPES, "_rels/.rels": _ROOT_RELS,
               "word/document.xml": f'<w:document xmlns:w="{_W_NS}"><w:body/></w:document>'}
    entries.update({f"word/media/pad{i}.bin": b"x" for i in range(count)})
    return make_zip(entries)


# ── Legacy / protected Word containers, damaged files ───────────────────────

def make_ole2_blob(*, encrypted_package: bool = False) -> bytes:
    """
    The bytes a legacy .doc starts with. A password-protected .docx is ALSO an OLE2 container and
    carries a stream called EncryptedPackage (stored UTF-16LE in the directory), hence the flag.
    """
    body = bytearray(OLE2_MAGIC + b"\x00" * 1016)
    if encrypted_package:
        body += "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64
    return bytes(body)


def truncate(data: bytes, keep: int) -> bytes:
    return data[:keep]


# ── multipart bodies, for tests that need byte-level control ────────────────

def multipart_body(boundary: str, files: Sequence[tuple], fields: Optional[dict] = None) -> bytes:
    """files: (field_name, filename, content_type, data). Returns a complete multipart/form-data body."""
    parts: List[bytes] = []
    for name, value in (fields or {}).items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, filename, content_type, data in files:
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
             f"Content-Type: {content_type}\r\n\r\n").encode() + data + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)
