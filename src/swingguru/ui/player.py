"""Streamlit wrapper for the guided playback component.

The player is a plain HTML/CSS/JS Streamlit custom component (the v1
``declare_component`` API, which serves the component's own directory over the
local Streamlit server). It needs no frontend framework, no CDN and no
network: the video preview and every asset live in
``src/swingguru/ui/player_assets`` and are served from the same loopback
server as the page.

Python sends the timeline (moments, checks, geometry) once. The component
posts a value back only when playback fails: any posted value reruns the whole
Streamlit script, so nothing is posted while the swing plays.
"""

from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

ASSETS_DIR = Path(__file__).resolve().parent / "player_assets"
GENERATED_DIR = ASSETS_DIR / "generated"
COMPONENT_NAME = "swingguru_guided_player"


def _component():
    """Declare the component lazily so importing this module is side-effect free."""

    global _COMPONENT
    if _COMPONENT is None:
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        _COMPONENT = components.declare_component(
            COMPONENT_NAME, path=str(ASSETS_DIR)
        )
    return _COMPONENT


_COMPONENT = None


def generated_dir() -> Path:
    """Directory the component serves previews from."""

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    return GENERATED_DIR


def render_player(payload: dict, *, key: str) -> dict | None:
    """Render the guided player and return the last value it reported."""

    value = _component()(data=payload, key=key, default=None)
    if isinstance(value, dict):
        return value
    return None


__all__ = ["ASSETS_DIR", "GENERATED_DIR", "generated_dir", "render_player"]
