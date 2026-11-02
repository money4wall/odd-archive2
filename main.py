#!/usr/bin/env python3
"""ODD ARCHIVE: resumable, cloud-first documentary video generator."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
WORK = ROOT / "work"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PIXABAY_URL = "https://pixabay.com/api/videos/"
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")
SCENE_COUNT = 10
MIN_REQUEST_INTERVAL = 12.0
_last_request_at = 0.0


def log(message: str) -> None:
    print(message, flush=True)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def topic_cache(topic: str) -> Path:
    digest = hashlib.sha256(topic.strip().encode("utf-8")).hexdigest()[:16]
    return WORK / digest


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"{result.stderr[-4000:]}"
        )


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    # Accept a JSON object followed or preceded by incidental model text.
    start = text.find("{")
    if start < 0:
        raise ValueError("Model response contains no JSON object")
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("Model response JSON is not an object")
    return value


def server_retry_seconds(headers: Any, body: str, attempt: int) -> float:
    if headers:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(2.0, float(retry_after) + 1.0)
            except ValueError:
                pass
    match = re.search(r"try again in\s+([0-9.]+)s", body, re.IGNORECASE)
    if match:
        return float(match.group(1)) + 1.5
    return min(60.0, 5.0 * (2 ** attempt))


def groq_json(prompt: str, max_tokens: int = 900) -> dict[str, Any]:
    global _last_request_at
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GROQ_API_KEY GitHub Actions secret")

    request_prompt = prompt + "\n\nOutput requirements: return exactly one valid JSON object. No markdown fences and no text outside the JSON."
    last_problem = "unknown error"

    for attempt in range(8):
        elapsed = time.monotonic() - _last_request_at
        if _last_request_at and elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You write factual documentary scripts. Follow the requested JSON structure exactly."},
                {"role": "user", "content": request_prompt},
            ],
            "temperature": 0.6,
            "max_completion_tokens": max_tokens,
            # GPT-OSS does not accept reasoning_format; use include_reasoning instead.
            "reasoning_effort": "low",
            "include_reasoning": False,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            GROQ_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ODD-ARCHIVE/1.0",
            },
            method="POST",
        )
        log(f"Groq request: attempt {attempt + 1}/8")
        try:
            with urllib.request.urlopen(req, timeout=150) as response:
                raw = response.read().decode("utf-8")
            _last_request_at = time.monotonic()
            data = json.loads(raw)
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            if choice.get("finish_reason") == "length":
                # GPT-OSS reasoning tokens count against max_completion_tokens.
                max_tokens = min(int(max_tokens * 1.5), 6000)
                log(f"Response was truncated; raising token budget to {max_tokens}")
            try:
                return parse_json_object(content)
            except (ValueError, json.JSONDecodeError) as exc:
                last_problem = f"Invalid model JSON: {exc}; response={content[:500]}"
                log(last_problem)
                request_prompt = (
                    "Your previous response was invalid. Return the requested content again "
                    "as one syntactically valid JSON object, with all required fields.\n\n"
                    + prompt
                )
                continue
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_problem = f"Groq HTTP {exc.code}: {body[:1200]}"
            log(last_problem)
            if exc.code == 429 or exc.code in (408, 500, 502, 503, 504):
                delay = server_retry_seconds(exc.headers, body, attempt)
                log(f"Waiting {delay:.1f}s before retry...")
                time.sleep(delay)
                continue
            raise RuntimeError(last_problem) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_problem = str(exc)
            delay = min(60.0, 5.0 * (2 ** attempt))
            log(f"Groq connection error: {exc}; waiting {delay:.1f}s")
            time.sleep(delay)
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unexpected Groq response: {exc}") from exc

    raise RuntimeError(f"Groq request failed after 8 attempts: {last_problem}")


def pick_topic(explicit: str | None) -> str:
    """Use the given topic, or rotate through topics.txt by GitHub run number."""
    if explicit and explicit.strip():
        return explicit.strip()
    path = ROOT / "topics.txt"
    topics = [t.strip() for t in path.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")] if path.exists() else []
    if not topics:
        raise RuntimeError("No topic given and topics.txt is missing or empty")
    run_number = int(os.getenv("GITHUB_RUN_NUMBER", "1") or 1)
    return topics[(run_number - 1) % len(topics)]


def generate_outline(topic: str, cache: Path) -> dict[str, Any]:
    path = cache / "outline.json"
    if path.exists():
        log("Restored saved outline")
        return load_json(path)

    prompt = f"""Create a factual English-language documentary package for ODD ARCHIVE.
Subject: {topic}
Style: restrained dark archival documentary, curious and atmospheric, not sensationalist.
Create exactly 10 scenes in a coherent narrative arc. Do not invent facts or quotations.
For each scene provide a short heading, a 1-2 sentence factual summary, and a concrete visual search query for stock footage.
Return JSON with exactly these top-level fields:
{{"title":"...","description":"2-3 short paragraphs","tags":["8-12 tags"],"thumbnail_text":"2-5 WORDS","scenes":[{{"heading":"...","summary":"...","search_query":"..."}}]}}
"""
    log("Groq: generating documentary outline...")
    result = groq_json(prompt, max_tokens=2500)
    scenes = result.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != SCENE_COUNT:
        raise RuntimeError(f"Outline must contain exactly 10 scenes; received {len(scenes) if isinstance(scenes, list) else 'invalid'}")
    for index, scene in enumerate(scenes, 1):
        if not all(isinstance(scene.get(k), str) and scene[k].strip() for k in ("heading", "summary", "search_query")):
            raise RuntimeError(f"Outline scene {index} is missing heading, summary or search_query")
    atomic_json(path, result)
    return result


def generate_scene(topic: str, brief: dict[str, Any], index: int, cache: Path) -> dict[str, Any]:
    scene_dir = cache / "scenes"
    path = scene_dir / f"scene_{index:02d}.json"
    if path.exists():
        saved = load_json(path)
        if isinstance(saved.get("narration"), str) and len(saved["narration"].split()) >= 65:
            log(f"Scene {index}/10 restored from cache")
            return saved

    prompt = f"""Write the spoken narration for scene {index} of 10 in a factual documentary.
Documentary subject: {topic}
Scene heading: {brief['heading']}
Scene brief: {brief['summary']}
Write 95-115 words of natural spoken English. Use concrete details, avoid invented facts, invented quotations, filler, headings and camera directions. Do not repeat an introduction. End with a smooth transition.
Return JSON: {{"narration":"..."}}
"""
    log(f"Groq: generating scene {index}/10...")
    result = groq_json(prompt, max_tokens=1500)
    narration = result.get("narration")
    if not isinstance(narration, str) or len(narration.split()) < 65:
        raise RuntimeError(f"Scene {index} narration is missing or too short")
    scene = {
        "heading": brief["heading"],
        "search_query": brief["search_query"],
        "narration": narration.strip(),
    }
    atomic_json(path, scene)
    log(f"Scene {index}/10 completed: {len(narration.split())} words; saved")
    return scene


def generate_package(topic: str) -> dict[str, Any]:
    cache = topic_cache(topic)
    cache.mkdir(parents=True, exist_ok=True)
    outline = generate_outline(topic, cache)
    scenes = [generate_scene(topic, brief, i, cache) for i, brief in enumerate(outline["scenes"], 1)]
    package = {k: outline.get(k) for k in ("title", "description", "tags", "thumbnail_text")}
    package["scenes"] = scenes
    atomic_json(cache / "package.json", package)
    return package


def usable_variant(hit: dict[str, Any]) -> dict[str, Any] | None:
    """First video size with a non-empty URL (Pixabay sometimes leaves 'large' empty)."""
    videos = hit.get("videos") or {}
    for size in ("large", "medium", "small"):
        variant = videos.get(size) or {}
        if variant.get("url"):
            return variant
    return None


def pick_video(hits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer up to 1080p; on ties keep Pixabay's own relevance order."""
    best, best_key = None, None
    for hit in hits:
        variant = usable_variant(hit)
        if not variant:
            continue
        key = (min(int(variant.get("width") or 0), 1920), min(int(variant.get("height") or 0), 1080))
        if best_key is None or key > best_key:
            best, best_key = variant, key
    return best


def pixabay_video(query: str, destination: Path) -> None:
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("Missing PIXABAY_API_KEY GitHub Actions secret")
    params = urllib.parse.urlencode({"key": key, "q": query, "video_type": "all", "per_page": 15, "safesearch": "true"})
    req = urllib.request.Request(PIXABAY_URL + "?" + params, headers={"User-Agent": "ODD-ARCHIVE/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Pixabay search failed for '{query}': {exc}") from exc
    hits = data.get("hits", [])
    video = pick_video(hits)
    if video is None:
        words = query.split()
        if len(words) > 1:
            shorter = " ".join(words[:-1])
            log(f"No usable footage for '{query}', retrying with '{shorter}'")
            pixabay_video(shorter, destination)
            return
        raise RuntimeError(f"Pixabay returned no usable footage for query: {query}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(video["url"], headers={"User-Agent": "ODD-ARCHIVE/1.0"})
    with urllib.request.urlopen(req, timeout=150) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    if destination.stat().st_size < 20_000:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded video is unexpectedly small: {destination.name}")


async def make_voice(text: str, destination: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate="-4%").save(str(destination))


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, check=True)
    duration = float(result.stdout.strip())
    if not (duration > 0):
        raise RuntimeError(f"Invalid media duration: {path}")
    return duration


def make_thumbnail(title: str, destination: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1280, 720), (12, 15, 19))
    draw = ImageDraw.Draw(image)
    draw.rectangle((52, 52, 1228, 668), outline=(117, 104, 82), width=3)
    draw.rectangle((72, 72, 1208, 648), outline=(54, 60, 65), width=1)
    font_candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf"]
    font_path = next((p for p in font_candidates if Path(p).exists()), None)
    font = ImageFont.truetype(font_path, 76) if font_path else ImageFont.load_default()
    small = ImageFont.truetype(font_path, 25) if font_path else ImageFont.load_default()
    lines = []
    for word in (title or "ODD ARCHIVE").upper().split():
        candidate = " ".join(lines[-1:] + [word])
        if lines and len(candidate) > 22:
            lines.append(word)
        elif lines:
            lines[-1] = candidate
        else:
            lines.append(word)
    lines = lines[:3]
    y = 240 if len(lines) == 1 else 190
    for line in lines:
        draw.text((100, y), line, font=font, fill=(232, 226, 211), stroke_width=1, stroke_fill=(0, 0, 0))
        y += 95
    draw.text((105, 600), "ODD ARCHIVE  /  DOCUMENTARY FILE", font=small, fill=(169, 157, 133))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, quality=94)


def render_scene(scene: dict[str, Any], index: int, cache: Path) -> Path:
    assets = cache / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    video = assets / f"scene_{index:02d}.mp4"
    audio = assets / f"scene_{index:02d}.mp3"
    segment = assets / f"scene_{index:02d}_render.mp4"
    if not video.exists() or video.stat().st_size < 20_000:
        log(f"[{index}/10] Pixabay: {scene['search_query']}")
        pixabay_video(scene["search_query"], video)
    if not audio.exists() or audio.stat().st_size < 1_000:
        log(f"[{index}/10] Generating voice")
        asyncio.run(make_voice(scene["narration"], audio))
    if segment.exists() and segment.stat().st_size > 50_000:
        return segment
    duration = ffprobe_duration(audio) + 0.4
    vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,fps=30,format=yuv420p"
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video), "-i", str(audio), "-t", f"{duration:.3f}", "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "192k", "-shortest", str(segment)])
    return segment


def build_video(package: dict[str, Any], topic: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    cache = topic_cache(topic)
    segments = [render_scene(scene, i, cache) for i, scene in enumerate(package["scenes"], 1)]
    concat = cache / "concat.txt"
    concat.write_text("".join(f"file '{p.resolve()}'\n" for p in segments), encoding="utf-8")
    safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "_", package.get("title") or "odd_archive").strip("_")[:70] or "odd_archive"
    final = OUT / f"{safe_title}.mp4"
    log("Assembling final documentary...")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(final)])
    if not final.exists() or final.stat().st_size < 100_000:
        raise RuntimeError("Final MP4 is missing or unexpectedly small")
    make_thumbnail(package.get("thumbnail_text") or package.get("title", "ODD ARCHIVE"), OUT / "thumbnail.jpg")
    atomic_json(OUT / "metadata.json", {k: package.get(k) for k in ("title", "description", "tags")})
    (OUT / "script.txt").write_text("\n\n".join(f"{s['heading']}\n\n{s['narration']}" for s in package["scenes"]), encoding="utf-8")
    return final


def self_test() -> None:
    import yaml
    workflow_path = ROOT / ".github" / "workflows" / "generate.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 parser interprets the GitHub key 'on' as boolean True.
    trigger = workflow.get("on", workflow.get(True))
    if not isinstance(trigger, dict) or "workflow_dispatch" not in trigger:
        raise RuntimeError("Workflow is missing workflow_dispatch trigger")
    required = ["GROQ_API_KEY", "PIXABAY_API_KEY", "ffmpeg", "ffprobe", "edge-tts"]
    source = "\n".join(p.read_text(encoding="utf-8") for p in [ROOT / "main.py", ROOT / "README.md", ROOT / "requirements.txt", workflow_path])
    missing = [item for item in required if item.lower() not in source.lower()]
    if missing:
        raise RuntimeError(f"Missing integration references: {missing}")
    for binary in ("ffmpeg", "ffprobe"):
        if subprocess.run(["which", binary], capture_output=True).returncode:
            raise RuntimeError(f"Required executable not found: {binary}")
    log("SELF-TEST OK: Python loaded, workflow parsed, integrations found, FFmpeg tools available.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default=os.getenv("TOPIC", ""))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    topic = pick_topic(args.topic)
    log(f"Generating ODD ARCHIVE episode: {topic}")
    package = generate_package(topic)
    final = build_video(package, topic)
    log(f"VIDEO_READY={final}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
