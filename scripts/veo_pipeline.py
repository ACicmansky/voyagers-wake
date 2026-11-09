#!/usr/bin/env python3
"""Generate normalized scene and connector clips from a motion-story manifest using Veo (Vertex AI)."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from scripts.veo_client import VeoClient, NonRetryableVeoError, build_veo_payload


def load_dotenv(root: Path) -> None:
    path = root / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        # also set in os.environ so subprocesses see it
        os.environ[key.strip()] = value.strip().strip("'\"")


def download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from urllib.request import Request, urlopen
    request = Request(url, headers={"User-Agent": "voyagers-wake/1.0"})
    with urlopen(request, timeout=180) as response, path.open("wb") as target:
        shutil.copyfileobj(response, target)


def run_ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def normalize_video(source: Path, target: Path, manifest: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    width = int(manifest.get("target_width", 1280))
    height = int(manifest.get("target_height", 720))
    fps = int(manifest.get("fps", 24))
    duration = float(manifest.get("duration", 4))
    filters = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},"
        f"tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},"
        "setpts=PTS-STARTPTS"
    )
    if manifest.get("generate_audio", False):
        audio_args = ["-af", f"apad,atrim=duration={duration}", "-c:a", "aac", "-b:a", "192k"]
    else:
        audio_args = ["-an"]
    run_ffmpeg([
        "-i", str(source), *audio_args, "-vf", filters,
        "-c:v", "libx264", "-preset", "slow", "-crf", str(manifest.get("crf", 20)),
        "-pix_fmt", "yuv420p", "-g", str(manifest.get("gop", 8)),
        "-keyint_min", str(manifest.get("gop", 8)), "-sc_threshold", "0",
        "-movflags", "+faststart", str(target),
    ])


def extract_frames(video: Path, first: Path, last: Path) -> None:
    first.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(video), "-vf", "select=eq(n\\,0)", "-frames:v", "1", str(first)])
    run_ffmpeg(["-sseof", "-0.05", "-i", str(video), "-frames:v", "1", str(last)])


def generate_clip(
    root: Path,
    client: VeoClient,
    manifest: dict[str, Any],
    kind: str,
    item: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    clip_id = item["id"]
    output = root / item["output"]
    if output.exists() and output.stat().st_size > 0 and not args.force:
        print(f"skip {kind} {clip_id} (already exists)")
        return

    prompt_path = root / item["prompt"]
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    style_motion_path = root / "prompts" / "style-motion.txt"
    if style_motion_path.exists():
        style_motion = style_motion_path.read_text(encoding="utf-8").strip()
        if style_motion:
            prompt = f"{style_motion}\n\n{prompt}"

    raw = root / "tmp" / "veo" / "raw" / kind / f"{clip_id}.mp4"
    tasks = root / "tmp" / "veo" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    safe = {
        "model": manifest.get("model", "veo-2.0"),
        "kind": kind,
        "clip_id": clip_id,
        "prompt_file": item["prompt"],
    }

    last_error: Exception | None = None
    for attempt in range(1, args.attempts + 1):
        print(f"{kind} {clip_id} attempt {attempt}/{args.attempts}", flush=True)
        try:
            payload = build_veo_payload(manifest, prompt)
            safe.update({"attempt": attempt, "payload": payload})
            (tasks / f"{kind}-{clip_id}-request.json").write_text(
                json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            operation_name = client.generate_video(payload)
            safe.update({"operation_name": operation_name})
            (tasks / f"{kind}-{clip_id}-operation.json").write_text(
                json.dumps({"operation_name": operation_name}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = client.wait_for_operation(operation_name, args.poll_seconds, args.timeout_seconds)
            (tasks / f"{kind}-{clip_id}-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            client.download_video(result, raw)
            normalize_video(raw, output, manifest)
            print(f"saved {output.relative_to(root)}", flush=True)
            return
        except Exception as exc:
            last_error = exc
            print(f"  failed: {exc}", file=sys.stderr, flush=True)
            if isinstance(exc, NonRetryableVeoError):
                raise
    raise RuntimeError(f"{kind} {clip_id} failed after {args.attempts} attempts") from last_error


def run_dives(root: Path, client: VeoClient, manifest: dict[str, Any], args: argparse.Namespace) -> None:
    items = manifest.get("dives", [])[: args.limit or None]
    frames = root / "assets" / "video" / "frames"

    def run_one(item: dict[str, Any]) -> None:
        generate_clip(root, client, manifest, "dives", item, args)
        output = root / item["output"]
        extract_frames(output, frames / f"{item['id']}-first.png", frames / f"{item['id']}-last.png")

    if items:
        with ThreadPoolExecutor(max_workers=min(args.workers, len(items))) as executor:
            list(executor.map(run_one, items))


def run_connectors(root: Path, client: VeoClient, manifest: dict[str, Any], args: argparse.Namespace) -> None:
    items = manifest.get("connectors", [])[: args.limit or None]
    frames = root / "assets" / "video" / "frames"

    def run_one(item: dict[str, Any]) -> None:
        generate_clip(root, client, manifest, "connectors", item, args)
        output = root / item["output"]
        extract_frames(output, frames / f"{item['id']}-first.png", frames / f"{item['id']}-last.png")

    if items:
        with ThreadPoolExecutor(max_workers=min(args.workers, len(items))) as executor:
            list(executor.map(run_one, items))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("phase", choices=["plan", "dives", "connectors", "all"], nargs="?", default="plan")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.workers < 1 or args.attempts < 1:
        parser.error("--workers and --attempts must be at least 1")

    root = args.project_root.expanduser().resolve()
    load_dotenv(root)
    manifest = json.loads((root / "prompts" / "video-manifest.json").read_text(encoding="utf-8"))
    plan = {
        "model": manifest.get("model", "veo-2.0"),
        "duration_seconds": manifest.get("duration_seconds", 5),
        "resolution": manifest.get("resolution", "720p"),
        "aspect_ratio": manifest.get("aspect_ratio", "16:9"),
        "person_generation": manifest.get("person_generation", "dont_allow"),
        "safety_filter_level": manifest.get("safety_filter_level", "block_some"),
        "dives": len(manifest.get("dives", [])),
        "connectors": len(manifest.get("connectors", [])),
        "workers": args.workers,
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.phase == "plan":
        return 0
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required")

    # Vertex AI credentials: prefer service account key file or ADC
    project_id = os.getenv("GCP_PROJECT_ID") or os.getenv("VERTEX_PROJECT_ID")
    location = os.getenv("GCP_LOCATION") or os.getenv("VERTEX_LOCATION") or "us-central1"
    if not project_id:
        raise SystemExit(
            "GCP_PROJECT_ID not set. For Vertex AI Veo, you need:\n"
            "  1. A Google Cloud project with Vertex AI API enabled\n"
            "  2. Either:\n"
            "     - GCP_SERVICE_ACCOUNT_KEY_FILE=/path/to/key.json (service account with Vertex AI User role)\n"
            "     - OR run `gcloud auth application-default login` for ADC\n"
            "  3. Set GCP_PROJECT_ID and optionally GCP_LOCATION in .env"
        )
    client = VeoClient(project_id, location)

    if args.phase in {"dives", "all"}:
        run_dives(root, client, manifest, args)
    if args.phase in {"connectors", "all"}:
        run_connectors(root, client, manifest, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())