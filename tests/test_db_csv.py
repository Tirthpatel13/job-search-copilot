"""CSV tracking file: lossless round trip, legacy-format import, merging into an existing DB."""

import csv
import shutil

from conftest import FIXTURES, ROOT, make_job

from jobcopilot.db import CSV_FIELDS, Database
from jobcopilot.models import Status

LEGACY_FIELDS = [
    "id",
    "title",
    "company",
    "location",
    "is_remote",
    "salary",
    "date_posted",
    "sources",
    "url",
    "apply_url",
    "score",
    "score_reason",
    "gaps",
    "status",
    "fetched_at",
    "scored_at",
]


def _rich_db(path):
    db = Database(path)
    db.upsert_jobs(
        [
            make_job(
                id="j1",
                description='Line one\nLine "two", with commas; and semicolons',
                seniority="senior",
                external_id="ext-1",
                source="lever",
                url="https://ca.indeed.com/viewjob?jk=1",
            ),
            make_job(id="j2", title="Data Engineer", source="indeed"),
        ]
    )
    db.upsert_jobs([make_job(id="j1", source="linkedin")])  # second source
    db.save_score("j1", 80, "Good.", ["k8s; helm", "Go"], ["Python", "SQL; dbt"])
    db.set_status("j1", Status.APPLIED)
    db.set_notes("j1", "Called recruiter;\nfollow up Friday")
    return db


def test_csv_columns_are_backward_compatible():
    assert CSV_FIELDS[: len(LEGACY_FIELDS)] == LEGACY_FIELDS


def test_export_import_is_lossless(tmp_path):
    db = _rich_db(tmp_path / "a.db")
    first = tmp_path / "first.csv"
    db.export_csv(first)

    fresh = Database(tmp_path / "b.db")
    assert fresh.import_csv(str(first)) == 2  # str paths are accepted
    job = fresh.get_job("j1")
    assert job.description == db.get_job("j1").description
    assert job.gaps == ["k8s; helm", "Go"]
    assert job.matched == ["Python", "SQL; dbt"]
    assert job.notes == "Called recruiter;\nfollow up Friday"
    assert (job.seniority, job.external_id, job.source) == ("senior", "ext-1", "lever")
    assert job.sources == ["lever", "linkedin"]
    assert (job.status, job.score) == (Status.APPLIED, 80)

    second = tmp_path / "second.csv"
    fresh.export_csv(str(second))
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_import_legacy_format(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.import_csv(FIXTURES / "legacy-evaluated-jobs.csv") == 2
    job = db.get_job("old1")
    assert job.sources == ["linkedin", "indeed"]
    assert job.source == "linkedin"
    assert job.gaps == ["Kubernetes", "Go"]
    assert (job.score, job.status, job.matched) == (74, Status.APPLIED, [])
    assert db.get_job("old2").sources == ["greenhouse"]


def test_committed_tracking_file_imports_and_round_trips(tmp_path):
    committed = ROOT / "data" / "evaluated-jobs.csv"
    with committed.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    copy = tmp_path / "evaluated-jobs.csv"
    shutil.copy(committed, copy)

    db = Database(tmp_path / "a.db")
    assert db.import_csv(copy) == len(rows)
    for row in rows[:20]:
        job = db.get_job(row["id"])
        assert job.title == row["title"]
        assert job.sources == [s for s in row["sources"].split(";") if s]

    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    db.export_csv(first)
    again = Database(tmp_path / "b.db")
    again.import_csv(first)
    again.export_csv(second)
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_import_fills_missing_evaluation_on_existing_rows(tmp_path):
    source = _rich_db(tmp_path / "a.db")
    path = tmp_path / "jobs.csv"
    source.export_csv(path)

    target = Database(tmp_path / "b.db")
    target.upsert_jobs([make_job(id="j1")])  # exists, unscored, default status
    target.import_csv(path)
    job = target.get_job("j1")
    assert (job.score, job.score_reason) == (80, "Good.")
    assert job.gaps == ["k8s; helm", "Go"] and job.matched == ["Python", "SQL; dbt"]
    assert job.scored_at
    assert job.status == Status.APPLIED
    assert job.notes == "Called recruiter;\nfollow up Friday"


def test_import_keeps_local_user_state(tmp_path):
    source = _rich_db(tmp_path / "a.db")
    path = tmp_path / "jobs.csv"
    source.export_csv(path)

    target = Database(tmp_path / "b.db")
    target.upsert_jobs([make_job(id="j1")])
    target.save_score("j1", 55, "Local.", [], [])
    target.set_status("j1", Status.INTERVIEW)
    target.set_notes("j1", "mine")
    target.import_csv(path)
    job = target.get_job("j1")
    assert (job.score, job.score_reason, job.status, job.notes) == (
        55,
        "Local.",
        Status.INTERVIEW,
        "mine",
    )


def test_import_missing_file_returns_zero(tmp_path):
    assert Database(tmp_path / "a.db").import_csv(str(tmp_path / "nope.csv")) == 0
