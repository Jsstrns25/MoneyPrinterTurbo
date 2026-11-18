import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.argstudio.models import Canon, Settings, merge_canon
from app.argstudio.store import Store
from app.argstudio.story import demo_story, generate


@pytest.fixture
def studio(tmp_path):
    return Store(tmp_path)


def test_idempotent_editorial_slots_under_concurrency(studio):
    sid = studio.create_series("Serie", "Premisa", Settings())
    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: studio.enqueue(sid, "2026-11-23"), range(12)))
    assert len(set(ids)) == 1
    assert len(studio.rows("jobs")) == 1


def test_canon_preserves_facts_and_resolves_mysteries():
    old = Canon(
        characters={"Ana": "Archivista"},
        events=["Radio encontrada"],
        open_mysteries=["Emisor"],
    )
    delta = Canon(
        characters={"Luis": "Operador"},
        events=["Radio encontrada", "Mensaje nuevo"],
        resolved_mysteries=["Emisor"],
    )
    result = merge_canon(old, delta)
    assert result.characters == {"Ana": "Archivista", "Luis": "Operador"}
    assert result.events == ["Radio encontrada", "Mensaje nuevo"]
    assert result.open_mysteries == []


def test_story_transaction_is_restartable(studio):
    sid = studio.create_series("Serie", "Premisa", Settings())
    eid = studio.enqueue(sid, "uno")
    generate(studio, studio.episode(eid))
    generate(studio, studio.episode(eid))
    assert studio.series(sid)["revision"] == 1
    assert {j["kind"] for j in studio.rows("jobs")} == {
        "story",
        "long",
        "short-0",
        "short-1",
    }
    assert json.loads(Store(studio.root).series(sid)["canon"])["characters"]["Mara"]


def test_stale_canon_cannot_be_committed(studio):
    sid = studio.create_series("Serie", "Premisa", Settings())
    first, second = (studio.enqueue(sid, slot) for slot in ["1", "2"])
    studio.save_story(first, demo_story("test"), 0)
    with pytest.raises(ValueError, match="Continuity"):
        studio.save_story(second, demo_story("test"), 0)
    assert studio.episode(second)["story"] is None


def test_budget_blocks_before_provider_and_retains_failed_estimate(studio):
    cfg = Settings(mode="live", allow_llm_requests=True, budget_usd=0.1)
    sid = studio.create_series("Serie", "Premisa", cfg)
    ep = studio.episode(studio.enqueue(sid, "1"))
    calls = []

    def bad_provider(prompt):
        calls.append(prompt)
        return "{}"

    with pytest.raises(ValidationError):
        generate(studio, ep, bad_provider)
    with pytest.raises(ValueError, match="budget"):
        generate(studio, ep, bad_provider)
    assert len(calls) == 1
    assert len(studio.rows("costs")) == 1


def test_no_llm_authorization_means_no_request(studio):
    sid = studio.create_series("Serie", "Premisa", Settings(mode="live"))
    with pytest.raises(ValueError, match="authorization"):
        generate(
            studio, studio.episode(studio.enqueue(sid, "1")), lambda _: pytest.fail()
        )
    assert studio.rows("costs") == []


def test_recover_and_retry_preserve_job_identity(studio):
    sid = studio.create_series("Serie", "Premisa", Settings())
    studio.enqueue(sid, "1")
    job = studio.claim()
    assert studio.claim() is None
    Store(studio.root).recover()
    assert studio.claim()["id"] == job["id"]
    studio.finish(job["id"], error="failed")
    studio.retry(job["id"])
    assert studio.claim()["id"] == job["id"]


@pytest.mark.parametrize(
    "values",
    [
        {"budget_usd": float("nan")},
        {"shorts_count": 1},
        {"music_file": "song.mp3"},
        {"width": 641},
    ],
)
def test_invalid_settings(values):
    with pytest.raises(ValidationError):
        Settings(**values)


def test_weekly_plan_is_idempotent_with_timezone(studio):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.argstudio.worker import plan_week

    sid = studio.create_series("Serie", "Premisa", Settings())
    first = plan_week(studio, sid, "2026-11-23")
    assert len(first) == 2
    assert first == plan_week(studio, sid, "2026-11-23")
    assert (
        datetime.fromtimestamp(
            studio.rows("jobs")[0]["due"], ZoneInfo("America/Panama")
        ).hour
        == 18
    )


def test_worker_lock_prevents_duplicate_processing(studio):
    from filelock import FileLock, Timeout
    from app.argstudio.worker import run

    with FileLock(str(studio.root / "worker.lock")):
        with pytest.raises(Timeout):
            run(studio, once=True)


def test_demo_renders_episode_and_two_shorts_and_reuses_cache(studio, monkeypatch):
    from app.argstudio import media
    from app.argstudio.worker import run

    sid = studio.create_series(
        "Demo", "Radio del futuro", Settings(demo_scene_seconds=1, width=320)
    )
    studio.enqueue(sid, "1")
    assert run(studio, once=True) == 4
    jobs = studio.rows("jobs")
    assert all(j["state"] == "complete" for j in jobs), jobs
    videos = [j for j in jobs if j["kind"] != "story"]
    for job in videos:
        result = json.loads(job["result"])
        duration, streams = media.probe(result["path"])
        stream = next(s for s in streams if s["codec_type"] == "video")
        assert stream["width"] / stream["height"] == pytest.approx(
            16 / 9 if job["kind"] == "long" else 9 / 16
        )
        assert duration > 0
        assert result["demo"] is True
    assert run(studio, once=True) == 0
    monkeypatch.setattr(
        media,
        "_render_scene",
        lambda *a: pytest.fail("Cached scenes were rendered again"),
    )
    assert (
        media.render(studio, videos[0])["sha256"]
        == json.loads(videos[0]["result"])["sha256"]
    )


def test_edge_adapter_uses_existing_tts_and_synchronized_captions(
    tmp_path, monkeypatch
):
    from app.argstudio import media
    from app.services import voice

    folder = tmp_path / "scene"
    folder.mkdir()
    marker = object()
    calls = []

    def tts(**kwargs):
        calls.append(kwargs)
        media.command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc",
                "-t",
                "1",
                "audio.mp3",
            ],
            folder,
        )
        return marker

    def captions(maker, text, path, word_level):
        assert maker is marker and word_level
        media.demo_subtitles(text, 1, __import__("pathlib").Path(path))

    monkeypatch.setattr(voice, "tts", tts)
    monkeypatch.setattr(voice, "create_subtitle", captions)
    assert media._audio(demo_story("x").scenes[0], Settings(mode="live"), folder) > 0
    assert calls[0]["voice_rate"] == 1.0
    assert calls[0]["voice_name"] == "es-PA-MargaritaNeural"


def publication_fixture(store, demo=False):
    from app.argstudio.media import digest

    sid = store.create_series("Serie", "Premisa", Settings())
    eid = store.enqueue(sid, "1")
    store.save_story(eid, demo_story("x"), 0)
    job = next(j for j in store.rows("jobs") if j["kind"] == "long")
    path = store.root / "video.mp4"
    path.write_bytes(b"test video bytes")
    store.finish(
        job["id"],
        result={
            "path": str(path),
            "sha256": digest(path),
            "title": "Title",
            "description": "Fiction",
            "tags": ["ARG"],
            "demo": demo,
        },
    )
    return job["id"], path


def test_approval_requires_explicit_decisions_and_blocks_demos(studio):
    from app.argstudio.youtube import approve

    jid, _ = publication_fixture(studio, demo=True)
    with pytest.raises(ValueError, match="Explicit"):
        approve(
            studio,
            jid,
            made_for_kids=None,
            contains_synthetic_media=False,
            rights_confirmed=True,
        )
    with pytest.raises(ValueError, match="demos"):
        approve(
            studio,
            jid,
            made_for_kids=False,
            contains_synthetic_media=True,
            rights_confirmed=True,
        )


class Response:
    def __init__(self, code=200, data=None, headers=None):
        self.status_code = code
        self.data = data or {}
        self.headers = headers or {}

    def json(self):
        return self.data


class Transport:
    def __init__(self, fail_after_accept=False):
        self.calls = []
        self.fail = fail_after_accept

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return Response(
            headers={
                "Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test"
            }
        )

    def put(self, url, **kwargs):
        self.calls.append(("put", url, kwargs))
        if self.fail:
            self.fail = False
            raise TimeoutError("Simulated loss of upload completion response")
        return Response(data={"id": "video-123"})


def test_private_upload_is_idempotent_and_resume_does_not_reupload(studio):
    from app.argstudio.youtube import approve, upload_private

    jid, _ = publication_fixture(studio)
    with pytest.raises(ValueError, match="approve"):
        upload_private(studio, jid, Transport())
    approve(
        studio,
        jid,
        made_for_kids=False,
        contains_synthetic_media=True,
        rights_confirmed=True,
    )
    http = Transport(fail_after_accept=True)
    with pytest.raises(TimeoutError):
        upload_private(studio, jid, http)
    assert studio.rows("publications")[0]["state"] == "uncertain"
    assert upload_private(studio, jid, http) == "video-123"
    assert upload_private(studio, jid, http) == "video-123"
    assert len([c for c in http.calls if c[0] == "post"]) == 1
    assert http.calls[0][2]["json"]["status"]["privacyStatus"] == "private"
    assert http.calls[-1][2]["headers"]["Content-Range"].startswith("bytes */")
    assert "session" not in studio.rows("publications")[0]


def test_changed_artifact_and_public_scheduling_are_blocked(studio):
    from app.argstudio.youtube import approve, schedule_publication, upload_private

    jid, path = publication_fixture(studio)
    approve(
        studio,
        jid,
        made_for_kids=False,
        contains_synthetic_media=False,
        rights_confirmed=True,
    )
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        upload_private(studio, jid, Transport())
    with pytest.raises(ValueError, match="authorization"):
        schedule_publication(studio, jid, "2099-01-01T00:00:00Z")


def test_expired_upload_session_never_starts_duplicate(studio):
    from app.argstudio.youtube import approve, upload_private

    jid, _ = publication_fixture(studio)
    approve(
        studio,
        jid,
        made_for_kids=False,
        contains_synthetic_media=False,
        rights_confirmed=True,
    )
    http = Transport(fail_after_accept=True)
    with pytest.raises(TimeoutError):
        upload_private(studio, jid, http)
    http.put = lambda *a, **k: Response(404)
    with pytest.raises(RuntimeError, match="reconcile"):
        upload_private(studio, jid, http)
    assert len([c for c in http.calls if c[0] == "post"]) == 1


def test_reject_and_bad_upload_destination(studio):
    from app.argstudio.youtube import approve, reject, upload_private, _session_url

    jid, _ = publication_fixture(studio)
    approve(
        studio,
        jid,
        made_for_kids=False,
        contains_synthetic_media=False,
        rights_confirmed=True,
    )
    reject(studio, jid)
    with pytest.raises(ValueError):
        upload_private(studio, jid, Transport())
    with pytest.raises(ValueError):
        _session_url("https://attacker.example/upload")


def test_failed_previous_story_blocks_later_continuity(studio):
    sid = studio.create_series("Serie", "Premisa", Settings())
    first = studio.enqueue(sid, "1", due=1)
    second = studio.enqueue(sid, "2", due=2)
    with pytest.raises(ValueError, match="previous episode"):
        generate(studio, studio.episode(second))
    generate(studio, studio.episode(first))
    generate(studio, studio.episode(second))
    assert studio.series(sid)["revision"] == 2


def test_live_story_contract_and_context(studio):
    cfg = Settings(mode="live", allow_llm_requests=True, budget_usd=0.1)
    sid = studio.create_series("Serie", "A fictional signal", cfg)
    episode = studio.episode(studio.enqueue(sid, "1"))
    result = demo_story("signal").model_dump()
    for scene in result["scenes"]:
        scene["narration"] = "Mara encuentra una señal extraña. " * 85
    for short in result["shorts"]:
        short["scene"]["narration"] = "Mara escucha una señal del futuro. " * 10

    def provider(prompt):
        assert "Existing canon" in prompt and "135 spoken words" in prompt
        return "```json\n" + json.dumps(result) + "\n```"

    generate(studio, episode, provider)
    assert studio.episode(episode["id"])["story"]
    assert studio.rows("costs")[0]["estimate"] == 0.1


def test_arg_streamlit_page_can_create_series(tmp_path, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("MPT_HOME", str(tmp_path))
    page = Path(__file__).resolve().parents[2] / "webui/pages/ARG_Studio.py"
    app = AppTest.from_file(str(page), default_timeout=30).run()
    assert not app.exception
    next(v for v in app.text_input if v.label == "Nombre").set_value("Mi ARG")
    next(v for v in app.text_area if v.label == "Premisa y estilo narrativo").set_value(
        "Señal del futuro"
    )
    next(b for b in app.button if b.label == "Crear serie").click().run()
    assert not app.exception
    assert Store(tmp_path / "storage/argstudio").rows("series")[0]["name"] == "Mi ARG"


def test_ideas_are_persisted_without_rewriting_canon(studio):
    from app.argstudio.story import suggest_ideas

    sid = studio.create_series("Ideas", "Misterios del espacio", Settings())
    result = suggest_ideas(studio, sid)
    assert len(result.items) == 3
    assert len(list((studio.root / "ideas" / sid).glob("*.json"))) == 1
    assert studio.series(sid)["revision"] == 0


def test_oauth_loopback_pkce_and_private_token_file(studio, monkeypatch, tmp_path):
    import base64
    import hashlib
    import threading
    from urllib.parse import parse_qs, urlencode, urlsplit
    import requests
    from app.argstudio import youtube

    client = tmp_path / "client.json"
    client.write_text(
        json.dumps(
            {"installed": {"client_id": "test-client", "client_secret": "test-secret"}}
        )
    )
    monkeypatch.setenv("MPT_YOUTUBE_CLIENT_FILE", str(client))
    seen = {}
    thread = None

    def browser(url):
        nonlocal thread
        query = parse_qs(urlsplit(url).query)
        seen.update(query)
        callback = (
            query["redirect_uri"][0]
            + "?"
            + urlencode({"state": query["state"][0], "code": "test-code"})
        )

        def deliver():
            with requests.Session() as http:
                http.trust_env = False
                assert http.get(callback, timeout=5).status_code == 200

        thread = threading.Thread(target=deliver)
        thread.start()
        return True

    def exchange(url, data, timeout):
        assert url == youtube.TOKEN
        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(data["code_verifier"].encode()).digest()
            )
            .decode()
            .rstrip("=")
        )
        assert seen["code_challenge"] == [expected]
        assert seen["code_challenge_method"] == ["S256"]
        return Response(
            data={
                "access_token": "test-only-access",
                "refresh_token": "test-only-refresh",
                "expires_in": 3600,
            }
        )

    monkeypatch.setattr(youtube.webbrowser, "open", browser)
    monkeypatch.setattr(youtube.requests, "post", exchange)
    youtube.authorize(studio)
    thread.join(timeout=5)
    path = studio.root / "auth/youtube.json"
    assert json.loads(path.read_text())["refresh_token"] == "test-only-refresh"
    if __import__("os").name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_explicit_scheduling_and_status(studio):
    from app.argstudio.youtube import (
        approve,
        upload_private,
        schedule_publication,
        inspect_upload,
    )

    jid, _ = publication_fixture(studio)
    approve(
        studio,
        jid,
        made_for_kids=False,
        contains_synthetic_media=True,
        rights_confirmed=True,
    )
    http = Transport()
    upload_private(studio, jid, http)
    schedule_publication(
        studio, jid, "2099-01-01T00:00:00Z", allow_public=True, transport=http
    )
    payload = http.calls[-1][2]["json"]
    assert payload["status"]["privacyStatus"] == "private"
    assert payload["status"]["containsSyntheticMedia"] is True
    assert payload["status"]["publishAt"] == "2099-01-01T00:00:00+00:00"
    assert studio.rows("publications")[0]["state"] == "scheduled"
    http.get = lambda *a, **k: Response(
        data={"items": [{"id": "video-123", "status": {"privacyStatus": "private"}}]}
    )
    assert inspect_upload(studio, jid, http)["items"][0]["id"] == "video-123"


def test_live_media_path_with_local_assets_and_licensed_audio(studio, monkeypatch):
    from app.argstudio import media
    from app.services import voice
    from pathlib import Path

    media.command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=24",
            "-t",
            "1",
            "source.mp4",
        ],
        studio.root,
    )
    media.command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1",
            "music.wav",
        ],
        studio.root,
    )
    settings = Settings(
        mode="live",
        visual_provider="local",
        local_visuals=[str(studio.root / "source.mp4")],
        asset_license_note="Generated test fixture",
        music_file=str(studio.root / "music.wav"),
        sound_effect_file=str(studio.root / "music.wav"),
        audio_license_note="Generated test tone",
        width=320,
    )
    sid = studio.create_series("Fixture", "Local media fixture", settings)
    eid = studio.enqueue(sid, "fixture")
    studio.save_story(eid, demo_story("fixture"), 0)
    job = next(j for j in studio.rows("jobs") if j["kind"] == "short-0")

    def tts(**kwargs):
        folder = Path(kwargs["voice_file"]).parent
        media.command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "audio.mp3",
            ],
            folder,
        )
        return object()

    def captions(maker, text, path, word_level):
        media.demo_subtitles(text, 1, Path(path))

    monkeypatch.setattr(voice, "tts", tts)
    monkeypatch.setattr(voice, "create_subtitle", captions)
    result = media.render(studio, job)
    assert result["demo"] is False
    assert result["audio_license_note"] == "Generated test tone"
    assert media.validate_video(result["path"]) > 0
    assert (Path(result["path"]).parent / "scene-000/sources.json").exists()
