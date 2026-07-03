"""Stable Diffusion XL image generation backend.

Calls any OpenAI-compatible /v1/images/generations endpoint (LiteLLM,
Automatic1111 with the --api flag, etc.).

Configure via environment variables (set in the .env secret):
  SDXL_BASE_URL   LiteLLM base URL, e.g. https://litellm.example.com/v1
  SDXL_API_KEY    API key (defaults to "none" for unauthenticated endpoints)
  SDXL_MODEL      Model name as registered in LiteLLM (default: stable-diffusion-xl-base-1.0)
  SDXL_TIMEOUT    Request timeout in seconds (default: 120)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)

# SDXL native resolutions that most schedulers handle well.
_SIZES: Dict[str, str] = {
    "landscape": "1216x832",
    "square": "1024x1024",
    "portrait": "832x1216",
}

_DEFAULT_MODEL = "stable-diffusion-xl-base-1.0"
_DEFAULT_TIMEOUT = 120.0


class SDXLImageGenProvider(ImageGenProvider):
    """Image generation via a self-hosted SDXL model exposed through LiteLLM."""

    @property
    def name(self) -> str:
        return "sdxl"

    @property
    def display_name(self) -> str:
        return "Stable Diffusion XL (cluster)"

    def _base_url(self) -> str:
        return os.environ.get("SDXL_BASE_URL", "").rstrip("/")

    def _api_key(self) -> str:
        return os.environ.get("SDXL_API_KEY", "none")

    def _model(self) -> str:
        return os.environ.get("SDXL_MODEL", _DEFAULT_MODEL)

    def _timeout(self) -> float:
        try:
            return float(os.environ.get("SDXL_TIMEOUT", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            return _DEFAULT_TIMEOUT

    def is_available(self) -> bool:
        return bool(self._base_url())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": self._model(),
                "display": "Stable Diffusion XL",
                "strengths": "Self-hosted, no per-image cost",
            }
        ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Stable Diffusion XL (cluster)",
            "badge": "self-hosted",
            "tag": "Self-hosted SDXL via LiteLLM — set SDXL_BASE_URL and SDXL_MODEL in .env",
            "env_vars": [
                {"key": "SDXL_BASE_URL", "prompt": "LiteLLM base URL (e.g. https://litellm.example.com/v1)"},
                {"key": "SDXL_API_KEY", "prompt": "API key (leave blank if unauthenticated)"},
                {"key": "SDXL_MODEL", "prompt": "Model name in LiteLLM"},
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
        if not base_url:
            return error_response(
                error="SDXL_BASE_URL is not set. Add it to your .env secret.",
                error_type="missing_config",
                provider=self.name,
                aspect_ratio=aspect_ratio,
            )

        aspect = resolve_aspect_ratio(aspect_ratio)
        size = _SIZES.get(aspect, "1024x1024")
        model = self._model()
        timeout = self._timeout()

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
        }

        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{base_url}/images/generations",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300] if resp is not None else str(exc)
            return error_response(
                error=f"SDXL request failed ({status}): {err_msg}",
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.Timeout:
            return error_response(
                error=f"SDXL timed out after {int(timeout)}s — the model may still be warming up.",
                error_type="timeout",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"Could not reach SDXL endpoint at {base_url}: {exc}",
                error_type="connection_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:
            return error_response(
                error=f"SDXL returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        data = result.get("data") or []
        if not data:
            return error_response(
                error="SDXL returned no image data.",
                error_type="empty_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0]
        b64 = first.get("b64_json")
        url = first.get("url")

        if b64:
            try:
                saved_path = save_b64_image(b64, prefix="sdxl_gen")
            except Exception as exc:
                return error_response(
                    error=f"Could not save generated image: {exc}",
                    error_type="io_error",
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        elif url:
            try:
                saved_path = save_url_image(url, prefix="sdxl_gen")
            except Exception as exc:
                logger.warning("SDXL image URL could not be cached (%s); using bare URL.", exc)
                image_ref = url
            else:
                image_ref = str(saved_path)
        else:
            return error_response(
                error="SDXL response contained neither b64_json nor url.",
                error_type="empty_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=image_ref,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
        )


def register(ctx: Any) -> None:
    """Plugin entry point — register SDXLImageGenProvider with Hermes."""
    ctx.register_image_gen_provider(SDXLImageGenProvider())
