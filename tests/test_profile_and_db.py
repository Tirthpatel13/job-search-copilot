import io

import pytest
from conftest import make_job
from docx import Document
from reportlab.pdfgen import canvas

from jobcopilot.models import Status
from jobcopilot.profile import ProfileError, extract_text, load_profile, save_resume_upload


def _docx_bytes(text: str) -> bytes:
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pdf_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("resume.md", b"# Jane Doe\nPython developer"),
        ("resume.txt", b"Jane Doe\nPython developer"),
        ("resume.docx", _docx_bytes("Jane Doe Python developer")),
        ("resume.pdf", _pdf_bytes("Jane Doe Python developer")),
    ],
)
def test_extract_text_supports_all_formats(name, data):
    assert "Jane Doe" in extract_text(name, data)


def test_extract_text_rejects_unknown_format():
    with pytest.raises(ProfileError):
        extract_text("resume.pages", b"")


def test_profile_falls_back_to_example(tmp_path):
    profile = load_profile(tmp_path / "nothing-here")
    assert profile.is_example
    assert "ShipFast Logistics" in profile.resume
    assert "Notice period" not in profile.answers or profile.answers
    assert "<experience_answers>" in profile.as_prompt()


def test_uploaded_resume_replaces_previous(tmp_path):
    save_resume_upload(tmp_path, "cv.md", b"Old")
    save_resume_upload(tmp_path, "My CV.docx", _docx_bytes("New resume"))
    assert not (tmp_path / "resume.md").exists()
    profile = load_profile(tmp_path)
    assert profile.resume_file == "resume.docx"
    assert profile.resume == "New resume"
    assert not profile.is_example


def test_upsert_preserves_user_state(db):
    db.upsert_jobs([make_job(id="j1", description="short")])
    db.save_score("j1", 81, "Good fit.", ["Kubernetes"], ["Python"])
    db.set_status("j1", Status.APPLIED)
    db.set_notes("j1", "Emailed recruiter")
    inserted, updated = db.upsert_jobs(
        [make_job(id="j1", description="a longer description", source="indeed")]
    )
    assert (inserted, updated) == (0, 1)
    job = db.get_job("j1")
    assert (job.score, job.status, job.notes) == (81, Status.APPLIED, "Emailed recruiter")
    assert job.description == "a longer description"
    assert job.sources == ["greenhouse", "indeed"]


def test_list_jobs_filters_and_sorts(db):
    db.upsert_jobs(
        [
            make_job(id="a", title="Backend Engineer", company="Zeta"),
            make_job(id="b", title="Data Engineer", company="Alpha", is_remote=True),
            make_job(id="c", title="Platform Engineer", company="Mid", source="lever"),
        ]
    )
    db.save_score("a", 90, "", [], [])
    db.save_score("b", 60, "", [], [])
    assert [j.id for j in db.list_jobs()] == ["a", "b", "c"]  # scored first, best first
    assert [j.id for j in db.list_jobs(min_score=70)] == ["a"]
    assert [j.id for j in db.list_jobs(q="data")] == ["b"]
    assert [j.id for j in db.list_jobs(remote=True)] == ["b"]
    assert [j.id for j in db.list_jobs(source="lever")] == ["c"]
    assert [j.id for j in db.list_jobs(sort="company")] == ["b", "c", "a"]
    assert db.stats()["total"] == 3 and db.stats()["scored"] == 2


def test_csv_roundtrip(db, tmp_path, settings):
    from jobcopilot.db import Database

    db.upsert_jobs([make_job(id="x1", salary="$100k")])
    db.save_score("x1", 77, "Solid match.", ["Go", "Kubernetes"], [])
    path = tmp_path / "data" / "evaluated-jobs.csv"
    assert db.export_csv(path) == 1

    fresh = Database(tmp_path / "fresh.db")
    assert fresh.import_csv(path) == 1
    job = fresh.get_job("x1")
    assert (job.score, job.gaps, job.salary) == (77, ["Go", "Kubernetes"], "$100k")
    assert fresh.unscored_jobs(10) == []  # nothing re-scored in the next Actions run
