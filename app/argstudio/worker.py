"""One worker, durable scheduling; no uploading from the production queue."""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from filelock import FileLock

from app.argstudio.media import render
from app.argstudio.story import generate


def plan_week(
    store, series_id, start_date, weekdays=(0, 3), hour=18, timezone="America/Panama"
):
    if (
        not weekdays
        or any(day not in range(7) for day in weekdays)
        or hour not in range(24)
    ):
        raise ValueError("Invalid weekly schedule")
    start = datetime.fromisoformat(start_date).date()
    zone = ZoneInfo(timezone)
    episodes = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        if day.weekday() in weekdays:
            due = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
            episodes.append(store.enqueue(series_id, day.isoformat(), due.timestamp()))
    return episodes


def run(store, once=False, max_jobs=None):
    completed = 0
    # A second process cannot steal a running job. OS releases lock after crashes.
    with FileLock(str(store.root / "worker.lock"), timeout=0):
        store.recover()
        while True:
            job = store.claim()
            if job is None:
                if once:
                    return completed
                time.sleep(2)
                continue
            try:
                if job["kind"] == "story":
                    story = generate(store, store.episode(job["episode_id"]))
                    result = {"title": story.title}
                else:
                    result = render(store, job)
                store.finish(job["id"], result=result)
            except Exception as exc:
                # Provider exceptions may contain URLs/tokens. Do not persist them.
                safe = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and type(exc).__module__ == "builtins"
                    else "Check provider configuration, connectivity, or media inputs; no automatic retry"
                )
                store.finish(job["id"], error=f"{type(exc).__name__}: {safe[:500]}")
            completed += 1
            if max_jobs is not None and completed >= max_jobs:
                return completed
