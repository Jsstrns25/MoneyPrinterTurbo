"""Run with .venv\Scripts\python.exe -m app.argstudio on Windows."""

import argparse
import json
import shutil
from pathlib import Path

from app.argstudio.models import Settings
from app.argstudio.store import Store
from app.argstudio.worker import plan_week, run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ARG Studio: local production with explicit private-upload approval"
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Separate local data directory (default storage/argstudio)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--premise", required=True)
    create.add_argument("--settings", type=Path, help="Credential-free Settings JSON")
    sub.add_parser("ideas").add_argument("series_id")
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("series_id")
    enqueue.add_argument("--slot", required=True)
    week = sub.add_parser("plan-week")
    week.add_argument("series_id")
    week.add_argument("--start", required=True, help="YYYY-MM-DD")
    week.add_argument("--weekdays", default="0,3", help="Monday=0, Sunday=6")
    week.add_argument("--hour", type=int, default=18)
    week.add_argument("--timezone", default="America/Panama")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true", help="Drain due jobs then exit")
    worker.add_argument("--max-jobs", type=int)
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("demo")
    retry = sub.add_parser("retry")
    retry.add_argument("job_id")
    approval = sub.add_parser("approve")
    approval.add_argument("job_id")
    approval.add_argument("--made-for-kids", choices=["yes", "no"], required=True)
    approval.add_argument("--synthetic", choices=["yes", "no"], required=True)
    approval.add_argument("--rights-confirmed", action="store_true", required=True)
    for action in ["reject", "upload-private", "upload-status", "thumbnail"]:
        sub.add_parser(action).add_argument("job_id")
    sub.add_parser("youtube-auth")
    schedule = sub.add_parser("schedule-publication")
    schedule.add_argument("job_id")
    schedule.add_argument("--at", required=True)
    schedule.add_argument("--allow-public", action="store_true")
    args = parser.parse_args(argv)
    store = Store(args.root)
    if args.command == "create":
        settings = (
            Settings.model_validate_json(args.settings.read_text(encoding="utf-8"))
            if args.settings
            else Settings()
        )
        print(store.create_series(args.name, args.premise, settings))
    elif args.command == "ideas":
        from app.argstudio.story import suggest_ideas

        print(suggest_ideas(store, args.series_id).model_dump_json(indent=2))
    elif args.command == "enqueue":
        print(store.enqueue(args.series_id, args.slot))
    elif args.command == "plan-week":
        print(
            plan_week(
                store,
                args.series_id,
                args.start,
                tuple(map(int, args.weekdays.split(","))),
                args.hour,
                args.timezone,
            )
        )
    elif args.command == "worker":
        run(store, once=args.once, max_jobs=args.max_jobs)
        if any(j["state"] == "failed" for j in store.rows("jobs")):
            return 1
    elif args.command == "retry":
        store.retry(args.job_id)
    elif args.command == "status":
        print(
            json.dumps(
                {
                    t: store.rows(t)
                    for t in ["series", "episodes", "jobs", "costs", "publications"]
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "doctor":
        import os

        for binary in ["ffmpeg", "ffprobe"]:
            print(binary, "available" if shutil.which(binary) else "MISSING")
        print(
            "YouTube OAuth client file configured:",
            bool(os.environ.get("MPT_YOUTUBE_CLIENT_FILE")),
        )
        print(
            "No network calls performed. Provider keys remain in existing MoneyPrinterTurbo configuration."
        )
    elif args.command == "demo":
        # Never drain a user's live queue as a side effect of running a demo.
        demo = Store(store.root / "demo")
        series = demo.rows("series")
        sid = (
            series[0]["id"]
            if series
            else demo.create_series(
                "Demo local", "Una radio recibe mensajes del futuro", Settings()
            )
        )
        eid = demo.enqueue(sid, "demo-001")
        run(demo, once=True)
        videos = [
            json.loads(j["result"])
            for j in demo.rows("jobs")
            if j["kind"] != "story" and j["state"] == "complete"
        ]
        if len(videos) != 3 or any(j["state"] == "failed" for j in demo.rows("jobs")):
            raise RuntimeError(
                "Demo failed; inspect status using --root " + str(demo.root)
            )
        print(
            json.dumps(
                {
                    "episode": eid,
                    "technical_demo_without_narration": True,
                    "videos": videos,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        from app.argstudio import youtube

        if args.command == "youtube-auth":
            youtube.authorize(store)
        elif args.command == "approve":
            youtube.approve(
                store,
                args.job_id,
                made_for_kids=args.made_for_kids == "yes",
                contains_synthetic_media=args.synthetic == "yes",
                rights_confirmed=args.rights_confirmed,
            )
        elif args.command == "reject":
            youtube.reject(store, args.job_id)
        elif args.command == "upload-private":
            print(youtube.upload_private(store, args.job_id))
        elif args.command == "upload-status":
            print(
                json.dumps(
                    youtube.inspect_upload(store, args.job_id), ensure_ascii=False
                )
            )
        elif args.command == "thumbnail":
            youtube.set_thumbnail(store, args.job_id)
        elif args.command == "schedule-publication":
            youtube.schedule_publication(
                store, args.job_id, args.at, allow_public=args.allow_public
            )


if __name__ == "__main__":
    raise SystemExit(main())
