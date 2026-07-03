"""Stable Diffusion XL image generation backend.

Calls the custom async job API exposed by sdxl-predictor:
  POST /generate              → {"job_id": "..."}
  GET  /progress/{job_id}    → poll until {"status": "completed", "image": "<base64>"}

Configure via environment variables (set in the .env secret):
  SDXL_BASE_URL       Service base URL (default: http://sdxl-predictor.cai-crew.svc.cluster.local:8080)
  SDXL_STEPS          Number of inference steps (default: 30)
  SDXL_GUIDANCE_SCALE Guidance scale (default: 8.0)
  SDXL_TIMEOUT        Total timeout in seconds for generation (default: 300)
  SDXL_POLL_INTERVAL  Seconds between progress polls (default: 3)
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://sdxl-predictor.cai-crew.svc.cluster.local:8080"

# SDXL native resolutions
_SIZES: Dict[str, tuple] = {
    "landscape": (1216, 832),
    "square":    (1024, 1024),
    "portrait":  (832, 1216),
}


class SDXLImageGenProvider(ImageGenProvider):
    """Image generation via the in-cluster SDXL predictor service."""

    @property
    def name(self) -> str:
        return "sdxl"

    @property
    def display_name(self) -> str:
        return "Stable Diffusion XL (cluster)"

    def _base_url(self) -> str:
        return os.environ.get("SDXL_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")

    def _steps(self) -> int:
        try:
            return int(os.environ.get("SDXL_STEPS", 30))
        except (TypeError, ValueError):
            return 30

    def _guidance_scale(self) -> float:
        try:
            return float(os.environ.get("SDXL_GUIDANCE_SCALE", 8.0))
        except (TypeError, ValueError):
            return 8.0

    def _timeout(self) -> float:
        try:
            return float(os.environ.get("SDXL_TIMEOUT", 300))
        except (TypeError, ValueError):
            return 300.0

    def _poll_interval(self) -> float:
        try:
            return float(os.environ.get("SDXL_POLL_INTERVAL", 3))
        except (TypeError, ValueError):
            return 3.0

    def is_available(self) -> bool:
        # No auth required — just needs the base URL to be reachable
        try:
            resp = requests.get(f"{self._base_url()}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "stable-diffusion-xl",
                "display": "Stable Diffusion XL",
                "strengths": "Self-hosted in-cluster, no per-image cost",
            }
        ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Stable Diffusion XL (cluster)",
            "badge": "self-hosted",
            "tag": "In-cluster SDXL via sdxl-predictor — set SDXL_BASE_URL if not in cai-crew namespace",
            "env_vars": [
                {"key": "SDXL_BASE_URL", "prompt": "Service base URL (default: in-cluster cai-crew address)"},
                {"key": "SDXL_STEPS", "prompt": "Inference steps (default: 30)"},
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        base_url = self._base_url()
        aspect = resolve_aspect_ratio(aspect_ratio)
        width, height = _SIZES.get(aspect, (1024, 1024))
        timeout = self._timeout()
        poll_interval = self._poll_interval()

        # --- Submit job ---
        payload = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_inference_steps": self._steps(),
            "guidance_scale": self._guidance_scale(),
        }

        try:
            resp = requests.post(
                f"{base_url}/generate",
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            try:
                msg = exc.response.json()
            except Exception:
                msg = exc.response.text[:300] if exc.response is not None else str(exc)
            return error_response(
                error=f"SDXL /generate failed ({status}): {msg}",
                error_type="api_error",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except Exception as exc:
            return error_response(
                error=f"SDXL /generate request failed: {exc}",
                error_type="connection_error",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            job_id = resp.json()["job_id"]
        except Exception:
            return error_response(
                error=f"SDXL returned unexpected response from /generate: {resp.text[:200]}",
                error_type="invalid_response",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        logger.info("[sdxl] Job submitted: %s (%dx%d, %d steps)", job_id, width, height, self._steps())

        # --- Poll for completion ---
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            try:
                poll_resp = requests.get(f"{base_url}/progress/{job_id}", timeout=15)
                poll_resp.raise_for_status()
                data = poll_resp.json()
            except Exception as exc:
                logger.warning("[sdxl] Poll error for job %s: %s", job_id, exc)
                continue

            status = data.get("status")
            if status == "completed":
                b64 = data.get("image")
                if not b64:
                    return error_response(
                        error="SDXL job completed but returned no image data.",
                        error_type="empty_response",
                        provider=self.name,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )
                try:
                    saved_path = save_b64_image(b64, prefix="sdxl_gen")
                except Exception as exc:
                    return error_response(
                        error=f"Could not save generated image: {exc}",
                        error_type="io_error",
                        provider=self.name,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )
                logger.info("[sdxl] Job %s completed → %s", job_id, saved_path)
                return success_response(
                    image=str(saved_path),
                    model="stable-diffusion-xl",
                    prompt=prompt,
                    aspect_ratio=aspect,
                    provider=self.name,
                )
            elif status in ("failed", "error"):
                return error_response(
                    error=f"SDXL job {job_id} failed: {data.get('message', 'unknown error')}",
                    error_type="api_error",
                    provider=self.name,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            else:
                step = data.get("step", "?")
                logger.debug("[sdxl] Job %s status=%s step=%s", job_id, status, step)

        return error_response(
            error=f"SDXL job {job_id} timed out after {int(timeout)}s.",
            error_type="timeout",
            provider=self.name,
            prompt=prompt,
            aspect_ratio=aspect,
        )


def register(ctx: Any) -> None:
    """Plugin entry point — register SDXLImageGenProvider with Hermes."""
    ctx.register_image_gen_provider(SDXLImageGenProvider())
