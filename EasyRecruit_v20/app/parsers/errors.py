"""
Typed failures for document parsing (Task 5).

Parsers know nothing about HTTP; app/uploads.py maps these onto status codes. Every
`public_message` is written for the end user: it never contains a temp-file path, a library
traceback or the raw text of an underlying exception. Technical detail goes to the server log
(`detail_for_log`) and nowhere else.
"""


class DocumentError(Exception):
    """A document that cannot be processed. `status_code` is the HTTP status it maps to."""

    status_code = 422

    def __init__(self, public_message: str, *, detail_for_log: str = ""):
        super().__init__(public_message)
        self.public_message = public_message
        self.detail_for_log = detail_for_log


class EmptyDocumentError(DocumentError):
    """Zero-byte upload."""
    status_code = 400


class UnsupportedDocumentError(DocumentError):
    """Wrong or unsupported type: bad extension / MIME, content that is not what the name claims, legacy .doc."""
    status_code = 415


class EncryptedDocumentError(DocumentError):
    """Password-protected / encrypted file."""
    status_code = 422


class MalformedDocumentError(DocumentError):
    """Damaged, truncated or otherwise unparseable file."""
    status_code = 422


class DocumentTooComplexError(DocumentError):
    """Within the upload size limit but too expensive to process (page count, expanded size, text volume)."""
    status_code = 422


class NoExtractableTextError(DocumentError):
    """Parsed fine but contains no (or too little) text, e.g. a scanned image."""
    status_code = 400


class DocumentTimeoutError(DocumentError):
    """Processing exceeded its time budget."""
    status_code = 504
