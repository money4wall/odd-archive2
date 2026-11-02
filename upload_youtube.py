#!/usr/bin/env python3
"""Optional YouTube upload using OAuth refresh-token credentials."""
import json, os, sys, urllib.parse, urllib.request
from pathlib import Path


def request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        return urllib.request.urlopen(req, timeout=180)
    except Exception as exc:
        detail = ""
        if hasattr(exc, "read"):
            try: detail = exc.read().decode(errors="replace")
            except Exception: pass
        raise RuntimeError(f"YouTube API request failed: {exc} {detail}") from exc


def main():
    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError("Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN")
    form = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode()
    with request("https://oauth2.googleapis.com/token", form, {"Content-Type": "application/x-www-form-urlencoded"}, "POST") as r:
        token_data = json.loads(r.read().decode())
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError(f"OAuth refresh did not return access_token: {token_data}")
    out = Path("output")
    video = next(out.glob("*.mp4"), None)
    if not video:
        raise RuntimeError("No MP4 found in output/")
    metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", "unlisted")
    if privacy not in {"private", "unlisted", "public"}:
        raise RuntimeError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public")
    body = {"snippet": {"title": metadata.get("title", "ODD ARCHIVE")[:100], "description": metadata.get("description", "")[:5000], "tags": metadata.get("tags", [])[:30], "categoryId": "22"}, "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(video.stat().st_size)}
    with request(init_url, json.dumps(body).encode(), headers, "POST") as r:
        upload_url = r.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube did not return a resumable upload URL")
    # The current implementation sends one resumable PUT body. GitHub-hosted
    # runners have ample RAM for typical short documentary files; the request
    # is still resumable at the YouTube protocol layer if the client is retried.
    with video.open("rb") as f:
        with request(upload_url, f.read(), {"Content-Type": "video/mp4", "Content-Length": str(video.stat().st_size)}, "PUT") as r:
            result = json.loads(r.read().decode())
    print(f"YOUTUBE_VIDEO_ID={result.get('id')}")
    print(f"YOUTUBE_URL=https://www.youtube.com/watch?v={result.get('id')}")


if __name__ == "__main__":
    try: main()
    except Exception as e:
        print(f"UPLOAD ERROR: {e}", file=sys.stderr); sys.exit(1)
