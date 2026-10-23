"""The user's profile folder: the only source of facts Claude may use.

Expected files (examples ship in `profile.example/`):

- `resume.pdf` | `resume.docx` | `resume.md` | `resume.txt`: base resume
- `criteria.md`: target roles, must-haves, deal-breakers
- `EXPERIENCE_ANSWERS.md`: vetted answers to common application questions
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

EXAMPLE_PROFILE_DIR = Path("profile.example")
RESUME_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")
MIN_RESUME_CHARS = 50
NO_TEXT_MESSAGE = (
    "Couldn't find text in that file. Scanned PDFs aren't supported; "
    "upload a DOCX, text-based PDF, or Markdown file."
)

# Characters XML (and so DOCX) cannot hold; tab, newline and carriage return are fine.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def strip_control_chars(text: str) -> str:
    """Remove control characters; form feeds and vertical tabs become line breaks."""
    return _CONTROL_CHARS.sub(lambda m: "\n" if m.group() in "\x0b\x0c" else "", text)


class ProfileError(RuntimeError):
    """Raised when the profile folder is missing required files."""


def _docx_text(data: bytes) -> str:
    """Body paragraphs and tables (nested, merged cells once) plus headers and footers."""
    from docx import Document
    from docx.table import Table

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    seen_cells: list = []  # elements, not id()s: lxml proxies are recycled

    def walk(container) -> None:
        for block in container.iter_inner_content():
            if isinstance(block, Table):
                for row in block.rows:
                    for cell in row.cells:
                        if any(cell._tc is tc for tc in seen_cells):
                            continue  # merged cells repeat the same <w:tc>
                        seen_cells.append(cell._tc)
                        walk(cell)
            else:
                lines.append(block.text)

    def walk_parts(attr: str) -> list[str]:
        out: list[str] = []
        seen: list = []
        for section in doc.sections:
            part = getattr(section, attr)
            if part.is_linked_to_previous or any(part._element is e for e in seen):
                continue
            seen.append(part._element)
            before = len(lines)
            walk(part)
            out += lines[before:]
            del lines[before:]
        return out

    headers = walk_parts("header")
    footers = walk_parts("footer")
    walk(doc)
    return "\n".join([*headers, *lines, *footers])


def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from a resume file's bytes based on its extension."""
    suffix = Path(filename).suffix.lower()
    if suffix not in RESUME_EXTENSIONS:
        raise ProfileError(f"Unsupported resume format {suffix!r}; use one of {RESUME_EXTENSIONS}")
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif suffix == ".docx":
            text = _docx_text(data)
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProfileError(f"{filename} is not a UTF-8 text file") from exc
    except Exception as exc:  # malformed PDF/DOCX: report it, don't crash the caller
        raise ProfileError(f"Couldn't read {filename}: {exc}") from exc
    return strip_control_chars(text).strip()


@dataclass(frozen=True)
class Profile:
    """Loaded profile content, ready to be placed in a prompt."""

    resume: str
    criteria: str
    answers: str
    directory: Path
    resume_file: str
    is_example: bool = False

    @property
    def resume_text(self) -> str:
        """The base resume: the only source for employers, schools and certifications."""
        return self.resume

    @property
    def facts(self) -> str:
        """Resume plus vetted answers. Excludes criteria.md (wishes, not facts)."""
        return "\n\n".join([self.resume, self.answers])

    @property
    def corpus(self) -> str:
        """Every fact the user has provided, used for fabrication checks."""
        return "\n\n".join([self.resume, self.criteria, self.answers])

    def as_prompt(self) -> str:
        """The profile as tagged sections for Claude."""
        return (
            f"<resume>\n{self.resume}\n</resume>\n\n"
            f"<target_criteria>\n{self.criteria}\n</target_criteria>\n\n"
            f"<experience_answers>\n{self.answers}\n</experience_answers>"
        )


def find_resume(directory: Path) -> Path | None:
    """First `resume.*` file with a supported extension, in preference order."""
    for ext in RESUME_EXTENSIONS:
        candidate = directory / f"resume{ext}"
        if candidate.exists():
            return candidate
    return None


def load_profile(directory: Path) -> Profile:
    """Load the profile folder, falling back to the shipped example profile."""
    is_example = directory.resolve() == EXAMPLE_PROFILE_DIR.resolve()
    if find_resume(directory) is None:
        log.warning(
            "No resume in %s; using the example profile in %s", directory, EXAMPLE_PROFILE_DIR
        )
        directory, is_example = EXAMPLE_PROFILE_DIR, True
    resume_path = find_resume(directory)
    if resume_path is None:
        raise ProfileError(f"No resume file found in {directory}")

    def read(name: str) -> str:
        path = directory / name
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    return Profile(
        resume=extract_text(resume_path.name, resume_path.read_bytes()),
        criteria=read("criteria.md"),
        answers=read("EXPERIENCE_ANSWERS.md"),
        directory=directory,
        resume_file=resume_path.name,
        is_example=is_example,
    )


def save_resume_upload(directory: Path, filename: str, data: bytes) -> Path:
    """Store an uploaded resume as `resume.<ext>`, replacing any previous resume.

    The previous resume is only removed once the new file is known to contain text.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in RESUME_EXTENSIONS:
        raise ProfileError(f"Unsupported resume format {suffix!r}")
    text = extract_text(filename, data)
    if len(re.sub(r"\s", "", text)) < MIN_RESUME_CHARS:
        raise ProfileError(NO_TEXT_MESSAGE)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"resume{suffix}"
    staging = directory / f".resume-upload{suffix}"
    staging.write_bytes(data)
    for ext in RESUME_EXTENSIONS:
        if ext != suffix:
            (directory / f"resume{ext}").unlink(missing_ok=True)
    staging.replace(target)
    return target
