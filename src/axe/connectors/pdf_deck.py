"""PDF pitch deck / CIM text extraction connector.

Prefers ``pymupdf`` (fitz) and falls back to ``pdfplumber`` when available.
The connector accepts a base64-encoded PDF file or file path in config and
returns a single ``IngestCandidate`` per page (or per document if requested).
"""

from __future__ import annotations

import base64
import io
import os

from axe.connectors.base import BaseConnector, ConnectorError, ConnectorResult, IngestCandidate


class PDFDeckConnector(BaseConnector):
    """Extract text from PDF pitch decks and CIMs."""

    source_type = "pdf_deck"

    async def fetch(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConnectorResult:
        """Extract text from a PDF document.

        Config keys:
          - file_content_b64: base64-encoded PDF bytes (optional)
          - file_path: path to a PDF file on disk (optional)
          - per_page: if True, emit one candidate per page (default True)
          - mime_type: optional MIME type hint

        Either ``file_content_b64`` or ``file_path`` must be provided.
        """
        file_content_b64 = self.get_config_value("file_content_b64")
        file_path = self.get_config_value("file_path")

        if file_content_b64:
            try:
                pdf_bytes = base64.b64decode(file_content_b64)
            except Exception as exc:
                raise ConnectorError(
                    f"Invalid base64 PDF content: {exc}",
                    source_type=self.source_type,
                    is_retryable=False,
                ) from exc
        elif file_path:
            if not os.path.exists(file_path):
                raise ConnectorError(
                    f"PDF file not found: {file_path}",
                    source_type=self.source_type,
                    is_retryable=False,
                )
            with open(file_path, "rb") as fh:
                pdf_bytes = fh.read()
        else:
            raise ConnectorError(
                "PDF connector requires file_content_b64 or file_path",
                source_type=self.source_type,
                is_retryable=False,
            )

        per_page = self.get_config_value("per_page", True)
        try:
            pages = self._extract_pages(pdf_bytes)
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(
                f"PDF extraction failed: {exc}",
                source_type=self.source_type,
                is_retryable=False,
            ) from exc

        candidates: list[IngestCandidate] = []
        if per_page:
            for page_num, text in enumerate(pages, start=1):
                if not text.strip():
                    continue
                candidates.append(self._build_candidate(page_num, text, pdf_bytes))
        else:
            full_text = "\n\n".join(pages)
            if full_text.strip():
                candidates.append(self._build_candidate(None, full_text, pdf_bytes))

        if limit is not None:
            candidates = candidates[:limit]

        return ConnectorResult(
            source_type=self.source_type,
            candidates=candidates,
            metadata={
                "pages": len(pages),
                "per_page": per_page,
                "mime_type": self.get_config_value("mime_type", "application/pdf"),
            },
        )

    def _build_candidate(
        self,
        page_num: int | None,
        text: str,
        pdf_bytes: bytes,
    ) -> IngestCandidate:
        source_label = f"pdf_page_{page_num}" if page_num is not None else "pdf_document"
        external_id = (
            f"{self.get_config_value('file_path', 'pdf')}#page={page_num}"
            if page_num is not None
            else self.get_config_value("file_path", "pdf")
        )
        return IngestCandidate(
            external_id=external_id,
            source_label=source_label,
            raw_payload_json={
                "page": page_num,
                "byte_size": len(pdf_bytes),
                "mime_type": self.get_config_value("mime_type", "application/pdf"),
            },
            extracted_signal_json={"text": text},
            content_text=text,
        )

    def _extract_pages(self, pdf_bytes: bytes) -> list[str]:
        """Extract text per page using the best available PDF library."""
        try:
            import fitz

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            pages = [page.get_text() or "" for page in doc]
            doc.close()
            return pages
        except ImportError:
            pass

        try:
            import pdfplumber

            extracted: list[str] = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    extracted.append(text)
            return extracted
        except ImportError as exc:
            raise ConnectorError(
                "PDF extraction requires pymupdf or pdfplumber",
                source_type=self.source_type,
                is_retryable=False,
            ) from exc
