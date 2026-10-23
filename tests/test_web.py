import base64
import shutil
from dataclasses import replace

import pytest
from conftest import FakeLLM, make_job
from fastapi.testclient import TestClient

from jobcopilot.db import Database
from jobcopilot.profile import EXAMPLE_PROFILE_DIR
from jobcopilot.scoring import JobScore
from jobcopilot.tailoring import EducationEntry, ExperienceEntry, JobRequirements, TailoredResume
from jobcopilot.web.main import create_app

RESUME = TailoredResume(
    name="Alex Rivera",
    headline="Backend Python Developer",
    contact=["Toronto, ON"],
    summary="Python developer.",
    skills=["Python", "FastAPI"],
    experience=[
        ExperienceEntry(
            title="Backend Developer",
            company="ShipFast Logistics",
            location="Toronto, ON",
            start="2022",
            end="Present",
            bullets=["Built FastAPI services."],
        )
    ],
    projects=[],
    education=[
        EducationEntry(
            credential="B.Sc.", institution="University of Waterloo", dates="2020", details=[]
        )
    ],
    certifications=[],
    review_items=[],
    change_log=[],
)


@pytest.fixture
def llm():
    return FakeLLM(
        {
            JobScore: JobScore(score=88, reason="Strong match.", matched=["Python"], gaps=["Go"]),
            JobRequirements: JobRequirements(
                required_skills=["Python"], nice_to_have=[], keywords=["FastAPI"]
            ),
            TailoredResume: RESUME,
        }
    )


@pytest.fixture
def client(settings, llm):
    shutil.copytree(EXAMPLE_PROFILE_DIR, settings.profile_dir)  # act as the user's own profile
    Database(settings.database_path).upsert_jobs([make_job(id="job1")])
    app = create_app(settings, llm_factory=lambda s: llm)
    with TestClient(app) as c:
        yield c


def test_index_lists_jobs(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Backend Engineer" in r.text
    assert 'rel="manifest"' in r.text


def test_filters_in_query_string(client):
    assert "Backend Engineer" not in client.get("/?q=nomatch").text
    assert client.get("/?min_score=abc").status_code == 200


def test_job_detail_and_status_change(client):
    r = client.get("/jobs/job1")
    assert r.status_code == 200
    assert "never applies for you" in r.text
    r = client.post("/jobs/job1/status", data={"status": "applied"}, follow_redirects=False)
    assert r.status_code == 303
    assert "chip-applied active" in client.get("/jobs/job1").text
    assert client.get("/?status=applied").text.count("job-card") == 1


def test_invalid_status_rejected(client):
    assert client.post("/jobs/job1/status", data={"status": "hired!"}).status_code == 400


def test_unknown_job_404(client):
    assert client.get("/jobs/nope").status_code == 404


def test_score_and_tailor_from_ui(client):
    client.post("/jobs/job1/score")
    page = client.get("/jobs/job1").text
    assert "Strong match." in page and "Gaps to review" in page

    r = client.post("/jobs/job1/tailor")
    assert r.status_code == 200
    assert "keyword match" in r.text
    link = r.text.split('href="/files/job1/')[1].split('"')[0]
    download = client.get(f"/files/job1/{link}")
    assert download.status_code == 200
    assert download.content[:2] == b"PK"  # DOCX is a zip


def test_file_download_blocks_path_traversal(client):
    assert client.get("/files/job1/..%2F..%2Fjobs.db").status_code == 404


def test_llm_errors_are_shown_not_raised(settings):
    from jobcopilot.llm import LLMError

    Database(settings.database_path).upsert_jobs([make_job(id="job1")])
    broken = FakeLLM({JobScore: LLMError("Claude declined this request")})
    with TestClient(create_app(settings, llm_factory=lambda s: broken)) as c:
        r = c.post("/jobs/job1/score")
    assert "Claude declined this request" in r.text


def test_profile_page_and_upload(settings, llm):
    resume = b"# Me\nPython developer with five years of FastAPI and PostgreSQL experience."
    with TestClient(create_app(settings, llm_factory=lambda s: llm)) as client:
        assert "Using the example profile" in client.get("/profile").text
        r = client.post("/profile/resume", files={"file": ("cv.md", resume, "text/markdown")})
        assert "Resume uploaded" in r.text
        assert "Using the example profile" not in r.text


def test_pwa_assets(client):
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert "serviceWorker" not in client.get("/sw.js").text  # served as the worker itself
    assert client.get("/static/icons/icon-192.png").status_code == 200
    assert client.get("/offline").status_code == 200


def test_fetch_status_endpoint(client):
    data = client.get("/api/fetch/status").json()
    assert data == {"running": False, "last_run": None}


def test_password_protection(settings, llm):
    app = create_app(replace(settings, app_password="s3cret"), llm_factory=lambda s: llm)
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/healthz").status_code == 200
        token = base64.b64encode(b"me:s3cret").decode()
        assert c.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
        bad = base64.b64encode(b"me:wrong").decode()
        assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_files_route_blocks_parent_directory(settings, client):
    (settings.output_dir.parent / ".env").write_text("ANTHROPIC_API_KEY=secret")
    settings.output_dir.mkdir(exist_ok=True)
    for url in ("/files/../.env", "/files/%2e%2e/.env", "/files/./x"):
        r = client.get(url)
        assert r.status_code == 404
        assert "secret" not in r.text


def test_non_ascii_password(settings, llm):
    app = create_app(replace(settings, app_password="pässword"), llm_factory=lambda s: llm)
    with TestClient(app) as c:
        good = base64.b64encode("me:pässword".encode()).decode()
        assert c.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
        bad = base64.b64encode("me:é".encode()).decode()
        assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_documents_refuse_example_profile(settings, llm):
    Database(settings.database_path).upsert_jobs([make_job(id="job1")])
    with TestClient(create_app(settings, llm_factory=lambda s: llm)) as c:
        r = c.post("/jobs/job1/tailor", follow_redirects=False)
        assert "Upload%20your%20resume" in r.headers["location"]
        assert "No resume uploaded yet" in c.get("/").text


def test_blank_questions_do_not_call_claude(client, llm):
    r = client.post("/jobs/job1/answers", data={"questions": "  \n "}, follow_redirects=False)
    assert "enter%20at%20least%20one%20question" in r.headers["location"]
    assert llm.calls == []


def test_unexpected_action_error_redirects_with_message(client, monkeypatch):
    import jobcopilot.web.main as main

    def boom(*args, **kwargs):
        raise ValueError("All strings must be XML compatible")

    monkeypatch.setattr(main, "tailor_resume", boom)
    r = client.post("/jobs/job1/tailor", follow_redirects=False)
    assert r.status_code == 303
    assert "something%20went%20wrong" in r.headers["location"]


def test_javascript_apply_link_not_rendered(settings, llm):
    Database(settings.database_path).upsert_jobs(
        [make_job(id="evil", url="javascript:alert(1)", apply_url="")]
    )
    with TestClient(create_app(settings, llm_factory=lambda s: llm)) as c:
        assert "javascript:alert" not in c.get("/jobs/evil").text


def test_unfinished_run_is_not_shown_as_no_fetch(settings, client):
    Database(settings.database_path).start_run()
    assert "A fetch is running" in client.get("/").text
