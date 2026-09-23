"""Turn raw resume text into structured JSON for matching."""

import logging

from llm import gemini

log = logging.getLogger(__name__)

PROMPT = """Extract structured data from the resume below.

Return strict JSON with exactly these keys:
{{
  "skills": [string],
  "years_experience": number,
  "past_titles": [string],
  "seniority": string,
  "education": [string],
  "summary": string
}}

Use null for anything the resume does not state. Do not infer or embellish.

--- RESUME ---
{text}
"""


async def parse_resume(resume_text: str) -> dict | None:
    if not resume_text or not resume_text.strip():
        return None
    if not gemini.available():
        log.warning("no Gemini key configured; skipping resume parsing")
        return None

    try:
        return await gemini.generate_json(
            PROMPT.format(text=resume_text[:30000]), priority=gemini.INTERACTIVE
        )
    except (ValueError, gemini.LLMError) as exc:
        log.warning("resume parsing failed: %s", exc)
        return None


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Extract raw text from a PDF or DOCX upload."""
    import io

    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        import pdfplumber

        chunks = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
        return "\n".join(chunks).strip()

    if lowered.endswith(".docx"):
        import docx

        document = docx.Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in document.paragraphs).strip()

    raise ValueError("unsupported file type; upload a PDF or DOCX")
