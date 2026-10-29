"""Free job scoring that runs locally, with no API key and no cost.

It compares the skills a posting asks for against the skills that appear in your
own resume, then adds points for title, location and seniority fit. It is less
nuanced than Claude's rubric, but it is deterministic, instant and explainable,
and it never claims a skill your profile does not show.
"""

from __future__ import annotations

import re

from .db import Database
from .models import Job
from .preferences import Preferences, infer_seniority, location_matches, title_matches
from .profile import Profile
from .scoring import JobScore
from .tailoring import _keyword_in

LABEL = "Free scorer"

# Skills and tools the scorer recognises. Order matters only for display.
SKILLS = [
    # languages
    "Python",
    "Java",
    "JavaScript",
    "TypeScript",
    "Go",
    "Golang",
    "Rust",
    "C++",
    "C#",
    "Ruby",
    "PHP",
    "Kotlin",
    "Swift",
    "Scala",
    "R",
    "SQL",
    "Bash",
    "Perl",
    "MATLAB",
    "Dart",
    "Elixir",
    "Haskell",
    "Lua",
    "Objective-C",
    "Solidity",
    "VBA",
    "COBOL",
    # web and backend
    "Django",
    "Flask",
    "FastAPI",
    "Spring",
    "Spring Boot",
    "Node.js",
    "Express",
    "NestJS",
    "React",
    "Angular",
    "Vue",
    "Next.js",
    "Svelte",
    "Redux",
    "HTML",
    "CSS",
    "Tailwind",
    "GraphQL",
    "REST",
    "gRPC",
    "WebSockets",
    "Rails",
    "Laravel",
    ".NET",
    "ASP.NET",
    "jQuery",
    "Microservices",
    "OAuth",
    # data stores and messaging
    "PostgreSQL",
    "MySQL",
    "SQLite",
    "SQL Server",
    "Oracle",
    "MongoDB",
    "Redis",
    "Cassandra",
    "DynamoDB",
    "Elasticsearch",
    "Snowflake",
    "BigQuery",
    "Redshift",
    "Databricks",
    "Kafka",
    "RabbitMQ",
    "Celery",
    "SQS",
    "Pub/Sub",
    "Neo4j",
    # data and ML
    "Pandas",
    "NumPy",
    "SciPy",
    "Spark",
    "PySpark",
    "Hadoop",
    "Airflow",
    "dbt",
    "Dagster",
    "Prefect",
    "ETL",
    "ELT",
    "Data Modeling",
    "Data Warehouse",
    "Tableau",
    "Power BI",
    "Looker",
    "Excel",
    "Machine Learning",
    "Deep Learning",
    "NLP",
    "Computer Vision",
    "PyTorch",
    "TensorFlow",
    "Keras",
    "scikit-learn",
    "LLM",
    "MLOps",
    "Statistics",
    "A/B Testing",
    "Jupyter",
    # cloud and devops
    "AWS",
    "Azure",
    "GCP",
    "Google Cloud",
    "Lambda",
    "EC2",
    "S3",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Ansible",
    "Helm",
    "CI/CD",
    "Jenkins",
    "GitHub Actions",
    "GitLab",
    "Git",
    "Linux",
    "Nginx",
    "Prometheus",
    "Grafana",
    "Datadog",
    "Serverless",
    "CloudFormation",
    # practices
    "Agile",
    "Scrum",
    "TDD",
    "Unit Testing",
    "Pytest",
    "Selenium",
    "Cypress",
    "Jest",
    "System Design",
    "Security",
    "Networking",
    "Distributed Systems",
]

# Variant spellings in postings that mean the same skill.
ALIASES = {
    "Golang": "Go",
    "Postgres": "PostgreSQL",
    "Google Cloud": "GCP",
    "K8s": "Kubernetes",
    "Sklearn": "scikit-learn",
    "React.js": "React",
    "ReactJS": "React",
    "Vue.js": "Vue",
    "Spring Boot": "Spring",
}

WEIGHTS = {"skills": 55, "title": 20, "location": 10, "seniority": 10, "keywords": 5}


def _canonical(skill: str) -> str:
    return ALIASES.get(skill, skill)


def skills_in(text: str) -> list[str]:
    """Known skills mentioned in `text`, canonical names, first-seen order."""
    found: list[str] = []
    for skill in [*SKILLS, *ALIASES]:
        name = _canonical(skill)
        if name not in found and _keyword_in(skill, text):
            found.append(name)
    return found


def _has(profile_text: str, skill: str) -> bool:
    variants = [skill, *(alias for alias, target in ALIASES.items() if target == skill)]
    return any(_keyword_in(v, profile_text) for v in variants)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z+#.]{3,}", text.lower())}


def score_job_free(profile: Profile, job: Job, prefs: Preferences) -> JobScore:
    """Score one job 0-100 from the profile and preferences, without any API."""
    facts = profile.facts or profile.corpus
    posting = f"{job.title}\n{job.description}"
    wanted = skills_in(posting)
    matched = [s for s in wanted if _has(facts, s)]
    gaps = [s for s in wanted if s not in matched]

    # With only a skill or two to go on (often just the title), lean towards a neutral
    # 50% so "1 of 1 skills" doesn't look like a perfect match.
    confidence = min(1.0, len(wanted) / 5)
    ratio = len(matched) / len(wanted) if wanted else 0.0
    skill_pts = WEIGHTS["skills"] * (ratio * confidence + 0.5 * (1 - confidence))
    title_ok = title_matches(job.title, prefs.titles)
    title_pts = WEIGHTS["title"] if title_ok else 0
    location_ok = location_matches(job, prefs)
    location_pts = WEIGHTS["location"] if location_ok else 0
    level = infer_seniority(job.title)
    seniority_ok = not prefs.seniority or level in prefs.seniority
    seniority_pts = WEIGHTS["seniority"] if seniority_ok else 0
    posting_words = _words(job.description)
    overlap = len(posting_words & _words(facts)) / len(posting_words) if posting_words else 0
    keyword_pts = WEIGHTS["keywords"] * min(1.0, overlap * 2)

    score = round(skill_pts + title_pts + location_pts + seniority_pts + keyword_pts)
    score = max(0, min(100, score))

    if wanted:
        shown = ", ".join(matched[:4]) or "none of them"
        skills_part = f"your resume shows {len(matched)} of {len(wanted)} listed skills ({shown})"
    else:
        skills_part = "the posting lists no skills the free scorer recognises"
    extras = []
    if not title_ok:
        extras.append("the title is outside your target roles")
    if not location_ok:
        extras.append("the location doesn't match your preferences")
    if not seniority_ok:
        extras.append(f"it looks like a {level} role")
    if not job.description.strip():
        extras.append("there's no job description, so this is a rough guess")
    reason = f"{LABEL}: {skills_part}" + (f"; {'; '.join(extras)}" if extras else "") + "."
    return JobScore(score=score, reason=reason, matched=matched[:6], gaps=gaps)


def score_pending_free(
    db: Database, profile: Profile, prefs: Preferences, limit: int = 5000
) -> dict[str, int]:
    """Score unscored jobs locally. It costs nothing, so the cap is generous."""
    done = 0
    for job in db.unscored_jobs(limit):
        result = score_job_free(profile, job, prefs)
        db.save_score(job.id, result.score, result.reason, result.gaps, result.matched)
        done += 1
    return {"scored": done}
