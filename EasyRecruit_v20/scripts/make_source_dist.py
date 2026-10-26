#!/usr/bin/env python3
"""
Build (and verify) a SAFE source distribution of EasyRecruit.

    python scripts/make_source_dist.py                       # -> dist/<project>-source.zip
    python scripts/make_source_dist.py --out my.zip
    python scripts/make_source_dist.py --verify some.zip     # audit any existing ZIP

Why this exists: .gitignore only governs git. A ZIP made by zipping the project
folder ignores it, which is how a populated database and a production env file
ended up in a distributed archive. This script refuses to package:

  * environment files (.env, .env.*, *.env) other than the safe templates
  * anything under app/data/ (SQLite databases, WAL files, uploaded profile photos)
  * SQLite databases by CONTENT (magic header), whatever the file is named
  * logs, private keys, virtualenvs, caches, VCS metadata
  * credential hand-out documents (e.g. CompanyAdmin_Credentials.docx)

and it re-verifies the finished archive before reporting success. Exit code 1
means a violation was found. Standard library only.
"""
import argparse
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

ALLOWED_ENV_TEMPLATES = {".env.example", ".env.production.example"}
FORBIDDEN_DIRS = {
    ".git", ".hg", ".svn", "venv", ".venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".cache", "node_modules", "dist", "build", "uploads",
    # Task 7: editor / coverage / lint / test-runner output that is never part of a release.
    ".idea", ".vscode", "htmlcov", ".tox", ".nox", ".ruff_cache", ".hypothesis", "temp_uploads",
}
FORBIDDEN_NAMES = {".coverage", ".ds_store", "thumbs.db"}
FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal",
    ".log", ".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx", ".swp", ".swo", ".orig", ".rej",
}
CREDENTIAL_DOC_SUFFIXES = {".docx", ".doc", ".xlsx", ".xls", ".pdf", ".csv", ".txt", ".rtf"}
SQLITE_MAGIC = b"SQLite format 3\x00"


def forbidden_reason(rel_path: str, head: Optional[bytes] = None) -> Optional[str]:
    """Why this archive-relative path must not be shipped, or None if it may be."""
    parts = [p for p in Path(rel_path.replace("\\", "/")).parts if p not in ("", ".")]
    if not parts:
        return None
    name, lower = parts[-1], parts[-1].lower()
    suffix = Path(lower).suffix

    for i in range(len(parts) - 1):                      # any app/data/ directory
        if parts[i] == "app" and parts[i + 1] == "data":
            return "populated data directory (app/data)"
    for d in parts[:-1]:
        if d in FORBIDDEN_DIRS or d.endswith(".egg-info"):
            return f"excluded directory ({d}/)"
    if lower in FORBIDDEN_NAMES or lower.startswith(".coverage."):
        return f"generated or OS file ({name})"
    if (lower == ".env" or lower.startswith(".env.") or lower.endswith(".env")) \
            and lower not in ALLOWED_ENV_TEMPLATES:
        return "environment file"
    if suffix in FORBIDDEN_SUFFIXES or lower.endswith((".db-wal", ".db-shm", ".db-journal")):
        return f"forbidden file type ({suffix or lower})"
    if lower.startswith(("id_rsa", "id_ed25519")):
        return "private key"
    if "credential" in lower and suffix in CREDENTIAL_DOC_SUFFIXES:
        return "credentials document"
    if head is not None and head.startswith(SQLITE_MAGIC):
        return "SQLite database (detected by content)"
    return None


def _head(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(SQLITE_MAGIC))
    except OSError:
        return b""


def verify_zip(zip_path: Path) -> List[Tuple[str, str]]:
    """Return [(member, reason)] for every member that must not be in a distribution."""
    bad = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as fh:
                head = fh.read(len(SQLITE_MAGIC))
            reason = forbidden_reason(info.filename, head)
            if reason:
                bad.append((info.filename, reason))
    return bad


def build(root: Path, out_zip: Path, top_dir: Optional[str] = None) -> Tuple[int, List[Tuple[str, str]]]:
    """Zip `root` minus everything forbidden. Returns (files_included, [(path, reason)] excluded)."""
    root, out_zip = root.resolve(), out_zip.resolve()
    top = top_dir or root.name
    included, excluded = 0, []
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_dir() or path.resolve() == out_zip:
                continue
            rel = path.relative_to(root).as_posix()
            reason = forbidden_reason(rel, _head(path) if path.is_file() else None)
            if reason:
                excluded.append((rel, reason))
                continue
            zf.write(path, f"{top}/{rel}")
            included += 1
    violations = verify_zip(out_zip)
    if violations:                                        # belt and braces
        out_zip.unlink(missing_ok=True)
        raise RuntimeError(f"refusing to emit archive with forbidden content: {violations}")
    return included, excluded


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--out")
    ap.add_argument("--verify", metavar="ZIP", help="audit an existing archive instead of building")
    args = ap.parse_args(argv)

    if args.verify:
        bad = verify_zip(Path(args.verify))
        for member, reason in bad:
            print(f"FORBIDDEN  {member}  ({reason})")
        print("FAILED" if bad else "OK: no forbidden content found.")
        return 1 if bad else 0

    root = Path(args.root)
    out = Path(args.out) if args.out else root / "dist" / f"{root.name}-source.zip"
    included, excluded = build(root, out)
    print(f"Wrote {out}  ({included} files)")
    print(f"Excluded {len(excluded)} path(s):")
    for rel, reason in excluded:
        if "__pycache__" not in rel:
            print(f"  - {rel}  [{reason}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
