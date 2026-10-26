"""
PDF Parser - Extract text from PDF files
Uses pdfplumber for text extraction
"""
import pdfplumber
import logging
import time
from typing import Optional

from app.parsers.errors import (
    DocumentTimeoutError, DocumentTooComplexError, EncryptedDocumentError,
    MalformedDocumentError, NoExtractableTextError,
)

try:
    from pdfminer.pdfdocument import PDFPasswordIncorrect
except ImportError:  # pragma: no cover - defensive: never fail at import time if pdfminer is laid out differently
    class PDFPasswordIncorrect(Exception):
        pass

logger = logging.getLogger(__name__)

_PDF_DAMAGED = "This file is not a valid PDF or it is damaged."


class PDFParser:
    """PDF text extraction using pdfplumber."""
    
    def __init__(self):
        """Initialize the PDF parser."""
        logger.info("PDF Parser initialized")
    
    def extract_text(self, file_path: str) -> Optional[str]:
        """
        Extract text from a PDF file.
        
        Args:
            file_path: Path to the PDF file
            
        Returns:
            Extracted text or None if extraction fails
        """
        try:
            text = ""
            
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    # Try standard extraction
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n\n"
                    else:
                        # Try extracting from tables if no direct text
                        tables = page.extract_tables()
                        for table in tables:
                            if table:
                                for row in table:
                                    if row:
                                        text += " ".join(str(cell) for cell in row if cell) + "\n"
            
            logger.info(f"Extracted {len(text)} characters from PDF")
            
            # If still no text, return a message
            if not text or len(text.strip()) == 0:
                logger.warning("PDF appears to be empty or image-based (scanned)")
                return "[PDF contains no extractable text - may be a scanned image or protected PDF]"
            
            return text.strip()
            
        except Exception as e:
            logger.error(f"Error extracting text from PDF: {e}")
            return None
    
    # ── Hardened extraction for untrusted uploads (Task 5) ────────────────────

    @staticmethod
    def _table_fallback_text(page) -> str:
        """The table fallback extract_text() uses for a page with no running text."""
        out = ""
        for table in page.extract_tables():
            if table:
                for row in table:
                    if row:
                        out += " ".join(str(cell) for cell in row if cell) + "\n"
        return out

    def extract_text_strict(
        self,
        file_path: str,
        *,
        max_pages: int = 50,
        max_chars: int = 300_000,
        deadline: Optional[float] = None,
    ) -> str:
        """
        Extract text from an UNTRUSTED PDF with hard bounds. Differences from extract_text():

          * never swallows an error: every failure is a typed DocumentError, so the caller can tell
            "password-protected" from "corrupt" from "scanned";
          * a PDF with no extractable text raises NoExtractableTextError instead of returning a
            placeholder sentence that downstream code would score as if it were a resume;
          * only the first max_pages + 1 pages are ever loaded, and more than max_pages is rejected;
          * the amount of text is capped, and `deadline` (a time.monotonic() value) is checked between
            pages so a slow document stops promptly instead of occupying a worker until it finishes.

        Messages in the raised errors are safe to show to the user; technical detail is in detail_for_log.
        """
        try:
            pdf = pdfplumber.open(file_path, pages=list(range(1, max_pages + 2)))
        except PDFPasswordIncorrect:
            raise EncryptedDocumentError(
                "This PDF is password-protected. Please remove the password and upload it again."
            ) from None
        except Exception as exc:                      # includes RecursionError from hostile object trees
            raise MalformedDocumentError(_PDF_DAMAGED, detail_for_log=f"{type(exc).__name__}: {exc}") from None

        with pdf:
            try:
                pages = pdf.pages
            except Exception as exc:
                raise MalformedDocumentError(_PDF_DAMAGED, detail_for_log=f"{type(exc).__name__}: {exc}") from None
            if not pages:
                raise MalformedDocumentError("This PDF contains no pages.")
            if len(pages) > max_pages:
                raise DocumentTooComplexError(
                    f"This PDF has more than {max_pages} pages, which is more than a resume needs. "
                    "Please upload a shorter version."
                )

            parts, total = [], 0
            for page in pages:
                if deadline is not None and time.monotonic() > deadline:
                    raise DocumentTimeoutError("Reading this PDF took too long and was stopped.")
                try:
                    page_text = page.extract_text()
                    chunk = (page_text + "\n\n") if page_text else self._table_fallback_text(page)
                except Exception as exc:
                    raise MalformedDocumentError(_PDF_DAMAGED, detail_for_log=f"{type(exc).__name__}: {exc}") from None
                if chunk:
                    total += len(chunk)
                    if total > max_chars:
                        raise DocumentTooComplexError(
                            "This document contains far more text than a resume should. "
                            "Please upload a shorter version."
                        )
                    parts.append(chunk)

        text = "".join(parts).strip()
        if not text:
            raise NoExtractableTextError(
                "No readable text was found in this PDF. It may be a scanned image; "
                "please upload a text-based PDF or a DOCX instead."
            )
        logger.info(f"Extracted {len(text)} characters from PDF (strict)")
        return text

    def extract_text_with_layout(self, file_path: str) -> Optional[dict]:
        """
        Extract text with layout information from a PDF file.
        
        Args:
            file_path: Path to the PDF file
            
        Returns:
            Dictionary with text and layout information
        """
        try:
            result = {
                'text': '',
                'pages': 0,
                'tables': [],
                'metadata': {}
            }
            
            with pdfplumber.open(file_path) as pdf:
                result['pages'] = len(pdf.pages)
                result['metadata'] = pdf.metadata or {}
                
                for i, page in enumerate(pdf.pages):
                    page_text = page.extract_text()
                    if page_text:
                        result['text'] += page_text + "\n\n"
                    
                    # Extract tables
                    tables = page.extract_tables()
                    for table in tables:
                        result['tables'].append({
                            'page': i + 1,
                            'table': table
                        })
            
            result['text'] = result['text'].strip()
            return result
            
        except Exception as e:
            logger.error(f"Error extracting text with layout from PDF: {e}")
            return None
    
    def extract_page_range(self, file_path: str, start_page: int = 0, end_page: Optional[int] = None) -> Optional[str]:
        """
        Extract text from a specific page range.
        
        Args:
            file_path: Path to the PDF file
            start_page: Start page (0-indexed)
            end_page: End page (exclusive, 0-indexed)
            
        Returns:
            Extracted text or None if extraction fails
        """
        try:
            text = ""
            
            with pdfplumber.open(file_path) as pdf:
                end_page = end_page or len(pdf.pages)
                
                for i in range(start_page, min(end_page, len(pdf.pages))):
                    page_text = pdf.pages[i].extract_text()
                    if page_text:
                        text += page_text + "\n\n"
            
            return text.strip() if text else None
            
        except Exception as e:
            logger.error(f"Error extracting page range from PDF: {e}")
            return None
