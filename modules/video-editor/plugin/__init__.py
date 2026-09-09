from __future__ import annotations

import logging
from typing import Any, Callable

from . import engine

logger = logging.getLogger(__name__)

PREPARE_SCHEMA = {
    "name": "video_editor_prepare",
    "description": "Prepare one or more raw talking-head videos for transcript-first editing. Transcribes locally, detects silence cut points, and returns a phrase reading view. Use this before making any cut decisions.",
    "parameters": {
        "type": "object",
        "properties": {
            "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8, "description": "Absolute local paths from this Hermes profile's cached videos or workspace."},
            "language": {"type": "string", "default": "ru", "description": "Pinned spoken language, e.g. ru or en. Never auto-detect."},
            "pacing": {"type": "string", "enum": ["punchy", "balanced", "restrained"], "default": "punchy"},
            "aspect": {"type": "string", "enum": ["9:16", "1:1", "16:9"], "default": "9:16"},
            "transcription_quality": {"type": "string", "enum": ["fast", "quality"], "default": "fast", "description": "fast uses pinned Whisper small; quality uses pinned Whisper medium and is much slower on CPU."},
        },
        "required": ["sources"],
    },
}

TAKES_SCHEMA = {
    "name": "video_editor_takes",
    "description": "Read another page of the prepared phrase transcript and silence cut points. Use when the prepare response did not contain enough transcript to plan the full edit.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 240, "default": 160},
            "source": {"type": "string", "description": "Optional source alias such as source_01 to limit returned cut-point metadata."},
        },
        "required": ["job_id"],
    },
}

RANGE_PROPERTIES = {
    "source": {"type": "string", "description": "Source alias from prepare, e.g. source_01."},
    "start": {"type": "number", "minimum": 0},
    "end": {"type": "number", "minimum": 0},
    "beat": {"type": "string"},
    "reason": {"type": "string"},
    "zoom": {"type": "number", "minimum": 1.0, "maximum": 1.8},
    "zoom_y": {"type": "number", "minimum": 0.0, "maximum": 1.0},
}

RENDER_SCHEMA = {
    "name": "video_editor_render",
    "description": "Render a model-authored EDL, then re-transcribe every seam and check repeated words/audio pops. The model must still read seam text for semantic clause completeness before presenting the cut.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "ranges": {"type": "array", "minItems": 1, "maxItems": 80, "items": {"type": "object", "properties": RANGE_PROPERTIES, "required": ["source", "start", "end"]}},
            "speed": {"type": "number", "minimum": 0.75, "maximum": 1.5, "default": 1.0},
            "grade": {"type": "string", "enum": ["none", "neutral_punch", "warm_lift"], "default": "none"},
            "preview": {"type": "boolean", "default": False},
        },
        "required": ["job_id", "ranges"],
    },
}

STATUS_SCHEMA = {
    "name": "video_editor_status",
    "description": "Return safe job state and local artifact paths for a prepared or rendered video edit.",
    "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
}

FEEDBACK_SCHEMA = {
    "name": "video_editor_feedback",
    "description": "Save an explicit owner correction as durable video-editing taste memory for future jobs. Preserve the user's original words in said when available.",
    "parameters": {
        "type": "object",
        "properties": {
            "area": {"type": "string", "enum": ["general", "cutting", "captions", "motion", "sound", "proof", "framing", "hooks"]},
            "instruction": {"type": "string"},
            "said": {"type": "string", "description": "Optional short verbatim user feedback for auditability."},
        },
        "required": ["area", "instruction"],
    },
}


def _guard(fn: Callable[..., dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    try:
        return fn(**args)
    except engine.VideoEditorError as exc:
        logger.warning("video editor tool failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def register(ctx: Any) -> None:
    ctx.register_tool(name="video_editor_prepare", toolset="video_editor", schema=PREPARE_SCHEMA,
                      handler=lambda args, **_: _guard(engine.prepare, args), check_fn=engine.runtime_ready,
                      description="Prepare talking-head footage into transcript + cut points.", emoji="🎬")
    ctx.register_tool(name="video_editor_takes", toolset="video_editor", schema=TAKES_SCHEMA,
                      handler=lambda args, **_: _guard(engine.read_takes, args), check_fn=engine.runtime_ready,
                      description="Page through prepared takes and silence cut points.", emoji="📝")
    ctx.register_tool(name="video_editor_render", toolset="video_editor", schema=RENDER_SCHEMA,
                      handler=lambda args, **_: _guard(engine.render, args), check_fn=engine.runtime_ready,
                      description="Render an EDL and verify every edit seam.", emoji="✂️")
    ctx.register_tool(name="video_editor_status", toolset="video_editor", schema=STATUS_SCHEMA,
                      handler=lambda args, **_: _guard(engine.status, args), check_fn=engine.runtime_ready,
                      description="Inspect a video-edit job and its artifacts.", emoji="📦")
    ctx.register_tool(name="video_editor_feedback", toolset="video_editor", schema=FEEDBACK_SCHEMA,
                      handler=lambda args, **_: _guard(engine.feedback, args), check_fn=engine.runtime_ready,
                      description="Persist explicit editing feedback for future videos.", emoji="🧠")
    logger.info("Shared video editor registered (runtime_ready=%s)", engine.runtime_ready())
