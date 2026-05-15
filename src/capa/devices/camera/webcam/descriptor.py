"""Setup-editor descriptor (plan §5.7) for :class:`WebcamAdapter`.

``DESCRIPTOR`` is registered into the global adapter registry as a
side effect of importing the package ``__init__``; this module just
holds the value and its params-model view.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from capa.devices.camera.webcam.adapter import WebcamAdapter
from capa.devices.camera.webcam.constants import DEFAULT_CODEC, DEFAULT_FPS, DEFAULT_PIX_FMT
from capa.devices.registry import AdapterDescriptor


class WebcamParams(BaseModel):
    """View model for :class:`WebcamAdapter`'s ``params`` dict (plan §4.9.3).

    Mirrors :meth:`WebcamAdapter.__init__`'s keyword arguments. Used by
    the Setup editor's Cameras section to produce a curated auto-form
    over otherwise free-form scalar params; not consulted at runtime
    (the adapter validates kwargs the existing way)."""

    model_config = ConfigDict(extra="ignore")

    fps: float = Field(default=DEFAULT_FPS, gt=0)
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    codec: str = DEFAULT_CODEC
    pix_fmt: str = DEFAULT_PIX_FMT
    input_url: str | None = None
    input_format: str | None = None


def _build_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="capa.devices.camera.webcam",
        label="USB webcam (visible)",
        family="camera_visible",
        adapter_factory=WebcamAdapter,
        params_model=WebcamParams,
        supported_binding_sources=(),  # Cameras don't bind via SourceBinding
        default_params={
            "fps": DEFAULT_FPS,
            "width": 1280,
            "height": 720,
            "codec": DEFAULT_CODEC,
            "pix_fmt": DEFAULT_PIX_FMT,
        },
        channel_templates=(),
        discoverable=True,
        handshake_available=True,
    )


DESCRIPTOR = _build_descriptor()
