"""Official OAuth + resumable private uploads. Never called by the worker."""

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from functools import wraps
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from filelock import FileLock

from app.argstudio.media import atomic_json, digest
from app.argstudio.store import dumps

API = "https://www.googleapis.com/youtube/v3"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def checked(response):
    if response.status_code >= 300:
        raise RuntimeError(
            f"YouTube/OAuth request failed (HTTP {response.status_code}); response omitted to protect credentials"
        )
    return response.json()


def _save_token(path, token):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, token)
    path.chmod(0o600)


def authorize(store):
    """Run on the user's machine with a Desktop OAuth client; never copy tokens."""
    client = json.loads(
        Path(os.environ["MPT_YOUTUBE_CLIENT_FILE"]).read_text(encoding="utf-8")
    )["installed"]
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            valid = query.get("state") == [state] and bool(query.get("code"))
            if valid:
                received["code"] = query["code"][0]
            self.send_response(200 if valid else 400)
            self.end_headers()
            self.wfile.write(
                b"Authorization received. You may close this window."
                if valid
                else b"Invalid authorization callback."
            )

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        if not webbrowser.open(url):
            raise RuntimeError(
                "Open this command on your Windows desktop with a working default browser"
            )
        deadline = time.monotonic() + 180
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received:
        raise TimeoutError("OAuth consent timed out")
    token = checked(
        requests.post(
            TOKEN,
            data={
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code": received["code"],
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
    )
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    _save_token(store.root / "auth" / "youtube.json", token)


def session(store):
    path = store.root / "auth" / "youtube.json"
    with FileLock(str(store.root / "oauth.lock")):
        token = json.loads(path.read_text(encoding="utf-8"))
        if token.get("expires_at", 0) < time.time() + 60:
            client = json.loads(
                Path(os.environ["MPT_YOUTUBE_CLIENT_FILE"]).read_text(encoding="utf-8")
            )["installed"]
            refreshed = checked(
                requests.post(
                    TOKEN,
                    data={
                        "client_id": client["client_id"],
                        "client_secret": client["client_secret"],
                        "refresh_token": token["refresh_token"],
                        "grant_type": "refresh_token",
                    },
                    timeout=30,
                )
            )
            token.update(refreshed)
            token["expires_at"] = time.time() + token.get("expires_in", 3600)
            _save_token(path, token)
    result = requests.Session()
    result.headers["Authorization"] = "Bearer " + token["access_token"]
    return result


def publication_lock(function):
    @wraps(function)
    def guarded(store, *args, **kwargs):
        with FileLock(str(store.root / "publication.lock"), timeout=0):
            return function(store, *args, **kwargs)

    return guarded


@publication_lock
def approve(
    store, job_id, *, made_for_kids, contains_synthetic_media, rights_confirmed
):
    if (
        type(made_for_kids) is not bool
        or type(contains_synthetic_media) is not bool
        or rights_confirmed is not True
    ):
        raise ValueError(
            "Explicit audience, synthetic-content and asset-rights decisions are required"
        )
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["state"] != "complete" or job["kind"] == "story":
            raise ValueError("Only completed video jobs can be approved")
        result = json.loads(job["result"])
        if result["demo"]:
            raise ValueError("Technical demos cannot be approved for upload")
        if digest(result["path"]) != result["sha256"]:
            raise ValueError("Artifact changed after rendering; review the new version")
        previous = db.execute(
            "SELECT * FROM publications WHERE job_id=?", (job_id,)
        ).fetchone()
        if previous and (previous["session"] or previous["video_id"]):
            raise ValueError(
                "Upload already started; approval cannot replace its metadata"
            )
        metadata = {
            "snippet": {
                "title": result["title"],
                "description": result["description"],
                "tags": result["tags"],
                "categoryId": "24",
            },
            "status": {
                "privacyStatus": "private",
                "selfDeclaredMadeForKids": made_for_kids,
                "containsSyntheticMedia": contains_synthetic_media,
            },
        }
        db.execute(
            "INSERT OR REPLACE INTO publications(job_id,state,metadata,sha256) VALUES(?,?,?,?)",
            (job_id, "approved", dumps(metadata), result["sha256"]),
        )


@publication_lock
def reject(store, job_id):
    with store.connect() as db:
        changed = db.execute(
            "UPDATE publications SET state='rejected' WHERE job_id=? AND session IS NULL AND video_id IS NULL",
            (job_id,),
        ).rowcount
        if not changed:
            raise ValueError(
                "Only an approved upload that has not started can be rejected"
            )


def _session_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.googleapis.com"
        or not parsed.path.startswith("/upload/")
    ):
        raise ValueError("Unexpected upload session destination")
    return url


def _record_video(store, job_id, response):
    video_id = checked(response).get("id")
    if not video_id:
        raise RuntimeError(
            "Upload response lacks a video id; reconcile before retrying"
        )
    with store.connect() as db:
        db.execute(
            "UPDATE publications SET state='private',video_id=?,error=NULL WHERE job_id=?",
            (video_id, job_id),
        )
    return video_id


def upload_private(store, job_id, transport=None):
    """Explicit command only. Resume existing sessions; never blindly restart them."""
    with FileLock(str(store.root / "publication.lock"), timeout=0):
        with store.connect() as db:
            record = db.execute(
                "SELECT * FROM publications WHERE job_id=?", (job_id,)
            ).fetchone()
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not record or record["state"] == "rejected":
            raise ValueError("Review and approve this video first")
        if record["video_id"]:
            return record["video_id"]
        result = json.loads(job["result"])
        path = Path(result["path"])
        if digest(path) != record["sha256"]:
            raise ValueError("Approved artifact has changed")
        http = transport or session(store)
        try:
            size = path.stat().st_size
            url = record["session"]
            offset = 0
            if url:
                response = http.put(
                    _session_url(url),
                    headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
                    timeout=60,
                    allow_redirects=False,
                )
                if response.status_code in (200, 201):
                    return _record_video(store, job_id, response)
                if response.status_code != 308:
                    raise RuntimeError(
                        "Upload session unavailable; manually reconcile in YouTube Studio to avoid duplicates"
                    )
                offset = (
                    int(response.headers.get("Range", "bytes=0--1").split("-")[-1]) + 1
                    if "Range" in response.headers
                    else 0
                )
            else:
                response = http.post(
                    "https://www.googleapis.com/upload/youtube/v3/videos",
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    json=json.loads(record["metadata"]),
                    headers={
                        "X-Upload-Content-Type": "video/mp4",
                        "X-Upload-Content-Length": str(size),
                    },
                    timeout=60,
                    allow_redirects=False,
                )
                if response.status_code not in (200, 201):
                    checked(response)
                url = _session_url(response.headers["Location"])
                with store.connect() as db:
                    db.execute(
                        "UPDATE publications SET session=?,state='uploading' WHERE job_id=?",
                        (url, job_id),
                    )
            with path.open("rb") as video:
                while offset < size:
                    video.seek(offset)
                    chunk = video.read(8 * 1024 * 1024)
                    response = http.put(
                        url,
                        data=chunk,
                        headers={
                            "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                        },
                        timeout=180,
                        allow_redirects=False,
                    )
                    if response.status_code in (200, 201):
                        return _record_video(store, job_id, response)
                    if response.status_code != 308 or "Range" not in response.headers:
                        raise RuntimeError(
                            "Upload outcome uncertain; retry queries the stored session"
                        )
                    next_offset = int(response.headers["Range"].split("-")[-1]) + 1
                    if next_offset <= offset or next_offset > size:
                        raise RuntimeError("Invalid upload acknowledgement")
                    offset = next_offset
            raise RuntimeError(
                "Upload lacks a final response; reconcile using the stored session"
            )
        except Exception:
            with store.connect() as db:
                db.execute(
                    "UPDATE publications SET state='uncertain',error='Query existing session; never restart blindly' WHERE job_id=?",
                    (job_id,),
                )
            raise
        finally:
            if transport is None:
                http.close()


def inspect_upload(store, job_id, transport=None):
    with store.connect() as db:
        record = db.execute(
            "SELECT video_id FROM publications WHERE job_id=?", (job_id,)
        ).fetchone()
    if not record or not record["video_id"]:
        raise ValueError("No confirmed video id")
    http = transport or session(store)
    try:
        return checked(
            http.get(
                API + "/videos",
                params={
                    "part": "status,processingDetails,snippet",
                    "id": record["video_id"],
                },
                timeout=30,
            )
        )
    finally:
        if transport is None:
            http.close()


def set_thumbnail(store, job_id, transport=None):
    with store.connect() as db:
        publication = db.execute(
            "SELECT video_id FROM publications WHERE job_id=?", (job_id,)
        ).fetchone()
        job = db.execute("SELECT result FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not publication or not publication["video_id"]:
        raise ValueError("Upload privately before setting a thumbnail")
    path = Path(json.loads(job["result"])["thumbnail"])
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Thumbnail exceeds 2 MB")
    http = transport or session(store)
    try:
        with path.open("rb") as image:
            return checked(
                http.post(
                    "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                    params={"videoId": publication["video_id"], "uploadType": "media"},
                    data=image,
                    headers={"Content-Type": "image/jpeg"},
                    timeout=60,
                )
            )
    finally:
        if transport is None:
            http.close()


@publication_lock
def schedule_publication(
    store, job_id, publish_at, *, allow_public=False, transport=None
):
    if allow_public is not True:
        raise ValueError(
            "Scheduling makes the video PUBLIC later; explicit authorization is required"
        )
    due = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
    if due.tzinfo is None or due <= datetime.now(timezone.utc):
        raise ValueError(
            "Publication time must be in the future with an explicit timezone"
        )
    with store.connect() as db:
        record = db.execute(
            "SELECT * FROM publications WHERE job_id=?", (job_id,)
        ).fetchone()
    if not record or not record["video_id"]:
        raise ValueError("Upload privately first")
    status = json.loads(record["metadata"])["status"]
    status.update(privacyStatus="private", publishAt=due.isoformat())
    http = transport or session(store)
    try:
        result = checked(
            http.put(
                API + "/videos",
                params={"part": "status"},
                json={"id": record["video_id"], "status": status},
                timeout=30,
            )
        )
        with store.connect() as db:
            db.execute(
                "UPDATE publications SET state='scheduled' WHERE job_id=?", (job_id,)
            )
        return result
    finally:
        if transport is None:
            http.close()
