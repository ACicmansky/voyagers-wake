#!/usr/bin/env python3
"""Google Veo (Vertex AI) API client for video generation."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class NonRetryableVeoError(RuntimeError):
    """An account or request error another identical attempt cannot fix."""


class VeoClient:
    """Client for Google Veo video generation via Vertex AI."""

    def __init__(
        self,
        project_id: str,
        location: str = "us-central1",
        base_url: str | None = None,
    ) -> None:
        self.project_id = project_id
        self.location = location
        # Vertex AI regional endpoint
        self.base_url = base_url or f"https://{location}-aiplatform.googleapis.com/v1"
        # Use ADC (Application Default Credentials) or GCP_SERVICE_ACCOUNT_KEY_FILE
        self._auth_header = self._build_auth_header()

    def _build_auth_header(self) -> str | None:
        # 1) Explicit service account key file
        key_file = os.environ.get("GCP_SERVICE_ACCOUNT_KEY_FILE")
        if key_file and os.path.exists(key_file):
            import google.oauth2.service_account
            creds = google.oauth2.service_account.Credentials.from_service_account_file(
                key_file, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(google.auth.transport.requests.Request())
            return f"Bearer {creds.token}"

        # 2) Application Default Credentials (gcloud auth application-default login)
        try:
            import google.auth
            import google.auth.transport.requests
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(google.auth.transport.requests.Request())
            return f"Bearer {creds.token}"
        except Exception:
            pass

        # 3) Fallback: legacy API key (won't work for Vertex AI, but kept for compat)
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("VEO_API_KEY")
        if api_key:
            return f"Bearer {api_key}"  # will likely 401/403 on Vertex

        return None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._auth_header:
            raise RuntimeError(
                "No valid Vertex AI credentials. Set GCP_SERVICE_ACCOUNT_KEY_FILE "
                "or run `gcloud auth application-default login`."
            )

        body = None if payload is None else json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}{path}"

        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._auth_header,
                "User-Agent": "voyagers-wake/1.0",
            },
        )
        try:
            with urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            fatal_codes = ("PERMISSION_DENIED", "UNAUTHENTICATED", "INVALID_ARGUMENT", "NOT_FOUND")
            error_type = NonRetryableVeoError if any(code in detail for code in fatal_codes) else RuntimeError
            raise error_type(f"Veo HTTP {exc.code}: {detail[:800]}") from exc
        except URLError as exc:
            raise RuntimeError(f"Veo network error: {exc.reason}") from exc

    def generate_video(self, payload: dict[str, Any]) -> str:
        """Start a Veo video generation task (Long-Running Operation)."""
        model = payload.pop("model", "veo-2.0")
        # Vertex AI publisher endpoint for Google models
        path = f"/projects/{self.project_id}/locations/{self.location}/publishers/google/models/{model}:predictLongRunning"
        result = self._request("POST", path, payload)
        if not result.get("name"):
            raise RuntimeError(f"Veo response missing operation name: {result}")
        return str(result["name"])

    def wait_for_operation(self, operation_name: str, poll_seconds: int, timeout_seconds: int) -> dict[str, Any]:
        """Poll the LRO until completion."""
        deadline = time.monotonic() + timeout_seconds
        # LRO name already fully qualified; just GET it
        while time.monotonic() < deadline:
            result = self._request("GET", f"/{operation_name}")
            done = result.get("done", False)
            print(f"  {operation_name}: {'done' if done else 'running'}", flush=True)
            if done:
                if "error" in result:
                    raise RuntimeError(f"Veo operation {operation_name} failed: {result['error']}")
                return result
            time.sleep(poll_seconds)
        raise TimeoutError(f"Veo operation {operation_name} timed out after {timeout_seconds}s")

    def download_video(self, operation_result: dict[str, Any], output_path: str) -> None:
        """Download the generated video from the operation result."""
        # Vertex AI LRO response: response.generatedVideos[].video.uri (signed GCS URL)
        response = operation_result.get("response", {})
        videos = response.get("generatedVideos", [])
        if not videos:
            raise RuntimeError("No generated videos in operation result")

        video_uri = videos[0].get("video", {}).get("uri")
        if not video_uri:
            raise RuntimeError("No video URI in generated video result")

        # The URI is a signed URL, download directly
        from scripts.seedance_pipeline import download
        download(video_uri, output_path)


def build_veo_payload(
    manifest: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    """Build the Veo API payload from manifest and prompt."""
    aspect_ratio = manifest.get("aspect_ratio", "16:9")
    duration_seconds = int(manifest.get("duration_seconds", 5))
    resolution = manifest.get("resolution", "720p")
    person_generation = manifest.get("person_generation", "dont_allow")
    safety_filter_level = manifest.get("safety_filter_level", "block_some")

    instance = {"prompt": prompt}

    parameters = {
        "aspectRatio": aspect_ratio,
        "durationSeconds": duration_seconds,
        "personGeneration": person_generation,
        "safetyFilterLevel": safety_filter_level,
    }

    # Veo 2.0 supports 720p; Veo 3.0 supports 1080p
    if resolution == "1080p":
        parameters["resolution"] = "1080p"

    payload = {
        "model": manifest.get("model", "veo-2.0"),
        "instances": [instance],
        "parameters": parameters,
    }
    return payload