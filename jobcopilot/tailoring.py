"""Truthful per-job resume tailoring, cover letters and application answers.

Pipeline for a resume:
1. Claude extracts the job's required skills and keywords.
2. Claude rewrites the resume from the profile only (reorder, rephrase, emphasise).
3. A deterministic verifier removes anything not traceable to the profile
   (unknown employers, schools, skills, certifications) and flags unverified numbers.
4. Keyword coverage is measured before and after so the user can see what changed.
5. ATS-friendly DOCX and PDF files are rendered.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .db import Database
from .documents import render_cover_letter_docx, render_resume_docx, render_resume_pdf
from .llm import StructuredLLM
from .models import Job
from .profile import Profile
from .scoring import job_prompt

# ---- schemas -------------------------------------------------------------------


class JobRequirements(BaseModel):
    """What the posting asks for, extracted by Claude."""

    required_skills: list[str] = Field(description="Hard requirements: skills, tools, credentials")
    nice_to_have: list[str] = Field(description="Preferred / bonus qualifications")
    keywords: list[str] = Field(description="ATS keywords and phrases, 1-3 words each")


class ExperienceEntry(BaseModel):
    title: str
    company: str
    location: str
    start: str
    end: str
    bullets: list[str]


class EducationEntry(BaseModel):
    credential: str
    institution: str
    dates: str
    details: list[str]


class ProjectEntry(BaseModel):
    name: str
    bullets: list[str]


class TailoredResume(BaseModel):
    """A complete resume, rewritten for one job from profile facts only."""

    name: str
    headline: str
    contact: list[str] = Field(description="Email, phone, city, links exactly as in the profile")
    summary: str
    skills: list[str]
    experience: list[ExperienceEntry]
    projects: list[ProjectEntry]
    education: list[EducationEntry]
    certifications: list[str]
    review_items: list[str] = Field(
        description="Job requirements the profile does not support, for the human to review"
    )
    change_log: list[str] = Field(description="What you changed versus the base resume and why")


class CoverLetter(BaseModel):
    greeting: str
    paragraphs: list[str]
    closing: str
    review_items: list[str] = Field(
        description="Claims you could not support from the profile or details to confirm"
    )


class AnswerItem(BaseModel):
    question: str
    answer: str = Field(description="Empty string if the profile does not answer it")
    source: str = Field(description="Which profile file/section the answer came from")
    needs_review: bool


class ApplicationAnswers(BaseModel):
    answers: list[AnswerItem]


EXTRACT_PROMPT = """\
Extract the requirements of the job below. Keep each item short (1-4 words) and use the
posting's own wording so they work as ATS keywords. Do not consider the candidate here."""

TAILOR_PROMPT = """\
Rewrite the candidate's resume for the job below.
- Keep every employer, title, date, school and credential exactly as in the profile.
- Reorder bullets and skills so the most relevant come first; rephrase bullets to use the
  job's terminology ONLY where the underlying experience is genuinely the same.
- Include a skill only if the profile states it. Keep metrics exactly as written.
- Use the target keywords where truthful; list unsupported requirements in review_items.
- headline: a short professional title that is true to the profile.
- Standard ATS sections only; no tables, columns or graphics (plain text fields).
"""

COVER_PROMPT = """\
Write a concise cover letter (3-4 short paragraphs, under 350 words) for the job below.
Every claim must come from the candidate profile. Do not invent motivation stories,
company knowledge beyond the posting, or metrics. Put anything you would need to confirm
in review_items. The letter will be reviewed by the candidate before any use."""

ANSWERS_PROMPT = """\
Answer each application question below using ONLY the candidate profile, preferring the
vetted text in <experience_answers>. If the profile does not answer a question, return an
empty answer with needs_review=true. Set needs_review=true whenever you had to adapt an
answer beyond light rephrasing. Never guess salary, availability, visa or legal answers."""


# ---- deterministic checks ------------------------------------------------------


def _fold(text: str) -> str:
    """NFKD-decompose and drop combining marks so 'Université' compares as 'Universite'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Casefold and collapse punctuation so 'Node.js' and 'node js' compare equal.

    Unicode-aware: letters in any script are kept, accents are dropped and '&' reads as
    'and'. The result is padded with spaces so `contains` can match whole terms.
    """
    folded = _fold(text).casefold().replace("&", " and ").replace("_", " ")
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w+#]+", " ", folded)).strip() + " "


# Scripts written without spaces between words (CJK, kana, Thai...) cannot use word padding.
_UNSPACED = re.compile(r"[฀-๿⺀-鿿가-힯豈-﫿ｦ-ﾟ]")


def contains(haystack_normalized: str, term: str, raw_haystack: str | None = None) -> bool:
    """Whole-term match against an already-normalized haystack.

    If the term has no letters or digits at all (e.g. a lone symbol) it falls back to a
    literal, case-insensitive search of `raw_haystack` instead of reporting "not found".
    """
    t = normalize(term)
    if t.strip() == "":
        raw = term.strip().casefold()
        return bool(raw) and raw_haystack is not None and raw in raw_haystack.casefold()
    if _UNSPACED.search(t):
        return t.strip() in haystack_normalized
    return t in haystack_normalized


def resume_to_text(r: TailoredResume) -> str:
    """Flatten a tailored resume to plain text (for keyword checks and previews)."""
    lines = [r.name, r.headline, " | ".join(r.contact), "", "SUMMARY", r.summary, ""]
    lines += ["SKILLS", ", ".join(r.skills), "", "EXPERIENCE"]
    for e in r.experience:
        lines.append(f"{e.title}, {e.company}, {e.location} ({e.start} - {e.end})")
        lines += [f"- {b}" for b in e.bullets]
    if r.projects:
        lines += ["", "PROJECTS"]
        for p in r.projects:
            lines.append(p.name)
            lines += [f"- {b}" for b in p.bullets]
    lines += ["", "EDUCATION"]
    for ed in r.education:
        lines.append(f"{ed.credential}, {ed.institution} ({ed.dates})")
        lines += [f"- {d}" for d in ed.details]
    if r.certifications:
        lines += ["", "CERTIFICATIONS", *r.certifications]
    return "\n".join(lines)


_NUMBER = re.compile(r"\$?\d[\d,.]*\s?(?:%|x|k|m|\+)?", re.I)
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")


def _canonical_numbers(text: str) -> set[str]:
    """Every number in `text` as a canonical string: '40,000' -> '40000', '010' -> '10'."""
    out = set()
    for raw in _DIGITS.findall(text):
        value = raw.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".") if re.fullmatch(r"\d+\.\d+", value) else value
        out.add(value.lstrip("0") or "0")
    return out


def unverified_numbers(text: str, known: set[str]) -> list[str]:
    """Figures in `text` whose numeric value never appears in the profile (exact match)."""
    bad = []
    for num in _NUMBER.findall(text):
        if not _canonical_numbers(num) <= known:
            bad.append(num.strip())
    return bad


_CONNECTORS = {"of", "de", "du", "des", "la", "le", "the", "for", "&"}


def _name_occurrences(text: str, term: str) -> list[bool]:
    """For each occurrence of `term` in `text` (line by line), whether it is a complete name.

    An occurrence is incomplete when it is glued (no punctuation in between) to another
    capitalised word, e.g. 'Logistics' inside 'ShipFast Logistics' or 'Waterloo' inside
    'University of Waterloo'.
    """
    keys = normalize(term).split()
    cased = any(ch.isupper() for ch in term)
    results: list[bool] = []
    if keys:
        for line in _fold(text).splitlines():
            results += _line_occurrences(line, keys, cased)
    return results


def _line_occurrences(line: str, keys: list[str], cased: bool) -> list[bool]:
    words = list(re.finditer(r"[\w+#]+|&", line))
    raw_words = [w.group() for w in words]
    tokens = ["and" if t == "&" else t.casefold() for t in raw_words]

    def glued(i: int, j: int) -> bool:
        gap = line[words[i].end() : words[j].start()]
        if gap.isspace() or re.fullmatch(r"[.'’-]+", gap):
            return True
        # 'Co. Ltd' or 'St. Mary' stay glued; a sentence-ending '. ' does not.
        return gap[:1] == "." and gap[1:].isspace() and len(raw_words[i]) <= 3

    def capital(i: int) -> bool:
        return raw_words[i][:1].isupper()

    def joins(conn: int, other: int) -> bool:
        """`conn` is a connector glued to a capitalised word at `other`."""
        return (
            raw_words[conn].casefold() in _CONNECTORS
            and glued(min(conn, other), max(conn, other))
            and capital(other)
        )

    results = []
    for i in range(len(tokens) - len(keys) + 1):
        if tokens[i : i + len(keys)] != keys:
            continue
        first, last = i, i + len(keys) - 1
        if cased and not any(capital(k) for k in range(first, last + 1)):
            continue  # 'logistics products' is not evidence for the name 'Logistics'
        partial = False
        if first > 0 and glued(first - 1, first):
            prev = first - 1
            partial = capital(prev) or (prev > 0 and joins(prev, prev - 1))
        if last + 1 < len(tokens) and glued(last, last + 1):
            nxt = last + 1
            partial = partial or capital(nxt)
            partial = partial or (nxt + 1 < len(tokens) and joins(nxt, nxt + 1))
        results.append(not partial)
    return results


@dataclass
class VerificationReport:
    """What the no-fabrication verifier removed or wants a human to check."""

    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def verify_resume(resume: TailoredResume, profile: Profile) -> VerificationReport:
    """Strip unsupported content from `resume` in place and report what happened.

    Employers, schools and certifications must appear as a whole phrase in the resume
    itself; skills in the resume or the vetted experience answers (never in criteria.md,
    which holds wishes and deal-breakers). A name that only appears as part of a longer
    name ('Logistics' for 'ShipFast Logistics') is kept but flagged. Titles, credentials,
    project names and dates are flagged when they are not in the resume, and every number
    in the resume is flagged unless the same number appears in the profile.
    """
    resume_raw = profile.resume
    resume_norm = normalize(resume_raw)
    facts_norm = normalize(profile.facts)
    known_numbers = _canonical_numbers(profile.facts)
    report = VerificationReport()

    def in_resume(term: str) -> bool:
        return contains(resume_norm, term, resume_raw)

    def check_name(kind: str, name: str) -> bool:
        if not in_resume(name):
            report.removed.append(f"{kind} not in profile: {name}")
            return False
        occurrences = _name_occurrences(resume_raw, name)
        if occurrences and not any(occurrences):
            report.warnings.append(
                f"{kind} name looks incomplete (only part of a longer name in the profile): {name}"
            )
        return True

    def flag(kind: str, value: str) -> None:
        if value.strip() and not in_resume(value):
            report.warnings.append(f"{kind} not in profile: {value}")

    resume.experience = [e for e in resume.experience if check_name("Employer", e.company)]
    for e in resume.experience:
        flag("Title", e.title)
        flag("Date", e.start)
        flag("Date", e.end)

    resume.education = [ed for ed in resume.education if check_name("School", ed.institution)]
    for ed in resume.education:
        flag("Credential", ed.credential)
        flag("Date", ed.dates)

    skills = []
    for s in resume.skills:
        if contains(facts_norm, s, profile.facts) or _compact_in(facts_norm, s):
            skills.append(s)
        else:
            report.removed.append(f"Skill not in profile: {s}")
    resume.skills = skills

    resume.certifications = [c for c in resume.certifications if check_name("Certification", c)]

    for p in resume.projects:
        flag("Project", p.name)

    texts = [resume.headline, resume.summary, *resume.skills, *resume.certifications]
    for e in resume.experience:
        texts += [e.title, e.location, *e.bullets]
    for p in resume.projects:
        texts += [p.name, *p.bullets]
    for ed in resume.education:
        texts += [ed.credential, *ed.details]
    for text in texts:
        for num in unverified_numbers(text, known_numbers):
            report.warnings.append(f"Unverified figure '{num}' in: {text[:90]}")
    return report


def _compact(term: str) -> str:
    return normalize(term).replace(" ", "")


def _keyword_in(keyword: str, text: str) -> bool:
    """Whether `keyword` appears in `text` as a whole term.

    Short keywords (<= 2 chars, e.g. 'R', 'Go', 'C#') and dotted ones ('.NET') are
    matched case-sensitively with strict boundaries; a lone letter also has to sit in
    a list-like context so 'Series C' or 'R&D' do not count. Everything else compares
    ignoring case, punctuation and spacing, so 'NodeJS' == 'Node.js' == 'node js'.
    """
    k = keyword.strip()
    if len(k) <= 2 or k.startswith("."):
        for m in re.finditer(r"(?<![\w.&+#/-])" + re.escape(k) + r"(?![\w&+#/-]|\.\w)", text):
            if len(k) > 1 or not k.isalpha():
                return True
            before = text[: m.start()]
            if re.search(r"(?:^|[,/(:;|•\n]|\b(?:and|or|in|with|using|including))\s*$", before):
                return True
        return False
    norm = normalize(text)
    if contains(norm, k, text):
        return True
    return _compact_in(norm, k)


def _compact_in(norm: str, term: str) -> bool:
    """Whether consecutive words of normalized `norm` spell `term` without spaces."""
    target = _compact(term)
    if not target:
        return False
    tokens = norm.split()
    for i in range(len(tokens)):
        joined = ""
        for tok in tokens[i:]:
            joined += tok
            if len(joined) >= len(target):
                break
        if joined == target:
            return True
    return False


def keyword_coverage(keywords: list[str], text: str) -> dict[str, Any]:
    """Which keywords appear in `text`, and the percentage covered."""
    unique: list[str] = []
    for k in (k.strip() for k in keywords):
        if k and k.lower() not in {u.lower() for u in unique}:
            unique.append(k)
    matched = [k for k in unique if _keyword_in(k, text)]
    missing = [k for k in unique if k not in matched]
    pct = round(100 * len(matched) / len(unique)) if unique else 0
    return {"percent": pct, "matched": matched, "missing": missing}


def keyword_report(
    reqs: JobRequirements, base_text: str, tailored_text: str, profile: Profile
) -> dict[str, Any]:
    """Before/after keyword coverage plus keywords that cannot be added truthfully."""
    keywords = [*reqs.required_skills, *reqs.keywords]
    before = keyword_coverage(keywords, base_text)
    after = keyword_coverage(keywords, tailored_text)
    return {
        "before": before,
        "after": after,
        "gained": [k for k in after["matched"] if k not in before["matched"]],
        "not_in_profile": [k for k in after["missing"] if not _keyword_in(k, profile.facts)],
    }


# ---- orchestration ---------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def extract_requirements(llm: StructuredLLM, profile: Profile, job: Job) -> JobRequirements:
    """Claude call 1: pull skills and keywords out of the posting."""
    return llm.generate(
        system=EXTRACT_PROMPT,
        profile=profile.as_prompt(),
        prompt=job_prompt(job),
        schema=JobRequirements,
    )


def tailor_resume(
    db: Database, llm: StructuredLLM, profile: Profile, job: Job, output_dir: Path
) -> dict[str, Any]:
    """Run the full tailoring pipeline for one job and store the result."""
    reqs = extract_requirements(llm, profile, job)
    prompt = (
        f"{job_prompt(job)}\n\n<target_keywords>\n"
        f"{', '.join([*reqs.required_skills, *reqs.keywords])}\n</target_keywords>"
    )
    resume = llm.generate(
        system=TAILOR_PROMPT, profile=profile.as_prompt(), prompt=prompt, schema=TailoredResume
    )
    verification = verify_resume(resume, profile)
    tailored_text = resume_to_text(resume)
    report = keyword_report(reqs, profile.resume, tailored_text, profile)

    folder = output_dir / job.id
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"resume-{_slug(job.company)}-{_slug(job.title)}"
    docx_path = render_resume_docx(resume, folder / f"{stem}.docx")
    pdf_path = render_resume_pdf(resume, folder / f"{stem}.pdf")

    payload = {
        "requirements": reqs.model_dump(),
        "resume": resume.model_dump(),
        "text": tailored_text,
        "verification": asdict(verification),
        "keywords": report,
        "files": {"docx": docx_path.name, "pdf": pdf_path.name},
    }
    db.add_artifact(job.id, "resume", payload)
    return payload


def draft_cover_letter(
    db: Database, llm: StructuredLLM, profile: Profile, job: Job, output_dir: Path
) -> dict[str, Any]:
    """Draft a grounded cover letter; always marked as needing human review."""
    letter = llm.generate(
        system=COVER_PROMPT,
        profile=profile.as_prompt(),
        prompt=job_prompt(job),
        schema=CoverLetter,
    )
    known = _canonical_numbers(profile.facts)
    warnings = [
        f"Unverified figure '{num}' in: {para[:90]}"
        for para in letter.paragraphs
        for num in unverified_numbers(para, known)
    ]
    letter.review_items.extend(warnings)
    text = "\n\n".join([letter.greeting, *letter.paragraphs, letter.closing])
    folder = output_dir / job.id
    folder.mkdir(parents=True, exist_ok=True)
    docx = render_cover_letter_docx(
        letter.greeting,
        letter.paragraphs,
        letter.closing,
        folder / f"cover-letter-{_slug(job.company)}.docx",
    )
    payload = {
        "letter": letter.model_dump(),
        "text": text,
        "review_required": True,
        "warnings": warnings,
        "files": {"docx": docx.name},
    }
    db.add_artifact(job.id, "cover_letter", payload)
    return payload


_SENSITIVE = re.compile(
    r"salar|compensation|\bpay\b|\bwage|visa|sponsor|work(?:ing)? authori[sz]|authori[sz]ed to work"
    r"|right to work|clearance|relocat|start date|when can you start|availab|disab|gender|\bsex\b"
    r"|pronoun|\brace\b|ethnic|veteran|military",
    re.I,
)


def answer_questions(
    db: Database, llm: StructuredLLM, profile: Profile, job: Job, questions: list[str]
) -> dict[str, Any]:
    """Answer application questions from EXPERIENCE_ANSWERS.md; flag anything uncovered.

    Every asked question gets exactly one answer, in the order asked. Questions the model
    skipped come back empty with needs_review. Sensitive topics (pay, visa, clearance,
    availability, demographics) and answers with figures not in the profile always need
    review.
    """
    qs = [q.strip() for q in questions if q and q.strip()]
    if not qs:
        return {"answers": [], "warnings": []}
    prompt = f"{job_prompt(job)}\n\n<questions>\n" + "\n".join(f"- {q}" for q in qs)
    prompt += "\n</questions>"
    result = llm.generate(
        system=ANSWERS_PROMPT,
        profile=profile.as_prompt(),
        prompt=prompt,
        schema=ApplicationAnswers,
    )
    by_question: dict[str, AnswerItem] = {}
    for item in result.answers:
        by_question.setdefault(normalize(item.question), item)
    asked = {normalize(q) for q in qs}
    unclaimed = {id(a) for a in result.answers if normalize(a.question) not in asked}
    same_shape = len(result.answers) == len(qs)

    known = _canonical_numbers(profile.facts)
    answers: list[AnswerItem] = []
    warnings: list[str] = []
    for idx, q in enumerate(qs):
        item = by_question.pop(normalize(q), None)
        if item is None and same_shape and id(result.answers[idx]) in unclaimed:
            item = result.answers[idx]  # model reworded the question but kept the order
        if item is None:
            item = AnswerItem(question=q, answer="", source="", needs_review=True)
        item = item.model_copy(update={"question": q})
        if not item.answer.strip() or _SENSITIVE.search(q):
            item.needs_review = True
        for num in unverified_numbers(item.answer, known):
            item.needs_review = True
            warnings.append(f"Unverified figure '{num}' in answer to: {q[:90]}")
        answers.append(item)
    payload = {"answers": [a.model_dump() for a in answers], "warnings": warnings}
    db.add_artifact(job.id, "answers", payload)
    return payload
