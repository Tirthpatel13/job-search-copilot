"""Command-line entry point.

python -m jobcopilot.cli run              # fetch + score (use from cron)
python -m jobcopilot.cli fetch            # fetch only
python -m jobcopilot.cli score            # score unscored jobs
python -m jobcopilot.cli tailor JOB_ID    # tailored resume DOCX/PDF
python -m jobcopilot.cli export data/evaluated-jobs.csv
python -m jobcopilot.cli serve            # start the web app
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .db import Database
from .llm import ClaudeLLM, LLMError
from .pipeline import run_pipeline
from .preferences import load_preferences
from .profile import load_profile
from .scoring import score_pending
from .tailoring import tailor_resume


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI."""
    p = argparse.ArgumentParser(prog="jobcopilot", description=__doc__.split("\n")[0])
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch and score")
    run.add_argument("--csv", type=Path, help="import this CSV first and export to it afterwards")
    sub.add_parser("fetch", help="fetch without scoring")
    score = sub.add_parser("score", help="score unscored jobs")
    score.add_argument("--limit", type=int, default=None)
    tailor = sub.add_parser("tailor", help="tailor the resume for one job")
    tailor.add_argument("job_id")
    export = sub.add_parser("export", help="export jobs to CSV")
    export.add_argument("path", type=Path)
    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    db = Database(settings.database_path)

    if args.command in ("run", "fetch"):
        csv_path: Path | None = getattr(args, "csv", None)
        if csv_path:
            print(f"Imported {db.import_csv(csv_path)} jobs from {csv_path}")
        summary = run_pipeline(settings, db, score=args.command == "run")
        print(json.dumps(summary, indent=2))
        if csv_path:
            print(f"Exported {db.export_csv(csv_path)} jobs to {csv_path}")
        return 0

    if args.command == "export":
        print(f"Exported {db.export_csv(args.path)} jobs to {args.path}")
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("jobcopilot.web.main:create_app", factory=True, host=args.host, port=args.port)
        return 0

    try:
        llm = ClaudeLLM(settings)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    profile = load_profile(settings.profile_dir)

    if args.command == "score":
        limit = args.limit or load_preferences(settings.preferences_file).max_jobs_to_score
        print(json.dumps(score_pending(db, llm, profile, limit), indent=2))
        return 0

    if args.command == "tailor":
        job = db.get_job(args.job_id)
        if job is None:
            print(f"error: no job with id {args.job_id}", file=sys.stderr)
            return 1
        result = tailor_resume(db, llm, profile, job, settings.output_dir)
        print(json.dumps({"files": result["files"], "keywords": result["keywords"]}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
