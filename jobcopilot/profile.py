"""The user's profile folder: the only source of facts Claude may use.

Expected files (examples ship in `profile.example/`):

- `resume.pdf` | `resume.docx` | `resume.md` | `resume.txt`: base resume
- `criteria.md`: target roles, must-haves, deal-breakers
- `EXPERIENCE_ANSWERS.md`: vetted answers to common application questions
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

EXAMPLE_PROFILE_DIR = Path("profile.example")
RESUME_EXTENSIONS = (".pdf", ".docx", ".md", ".txt")


class ProfileError(RuntimeError):
    """Raised when the profile folder is missing required files."""


def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from a resume file's bytes based on its extension."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if suffix == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs).strip()
    if suffix in (".md", ".txt"):
        return data.decode("utf-8", errors="replace").strip()
    raise ProfileError(f"Unsupported resume format {suffix!r}; use one of {RESUME_EXTENSIONS}")


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
    is_example = False
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
    """Store an uploaded resume as `resume.<ext>`, replacing any previous resume."""
    suffix = Path(filename).suffix.lower()
    if suffix not in RESUME_EXTENSIONS:
        raise ProfileError(f"Unsupported resume format {suffix!r}")
    extract_text(filename, data)  # validate it parses before replacing anything
    directory.mkdir(parents=True, exist_ok=True)
    for ext in RESUME_EXTENSIONS:
        (directory / f"resume{ext}").unlink(missing_ok=True)
    target = directory / f"resume{suffix}"
    target.write_bytes(data)
    return target
