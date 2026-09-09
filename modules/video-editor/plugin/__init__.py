from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import engine, enrichment
from .phase2_schemas import (CAPTIONS_SCHEMA, CAPTION_APPROVE_SCHEMA, CARDS_SCHEMA, CAPTURE_SCHEMA, PROOF_SCHEMA, SOUND_SCHEMA, MASTER_SCHEMA)
from .look_schema import LOOK_SCHEMA

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
            "asr_provider": {"type": "string", "enum": ["auto", "openrouter", "local"], "default": "auto", "description": "auto prefers the shared OpenRouter Whisper Turbo broker and falls back to local whisper.cpp; openrouter requires broker success; local is offline-only."},
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


def _guard(fn: Callable[..., dict[str, Any]], args: dict[str, Any]) -> str:
    """Return the JSON-string tool result required by Hermes' agent registry."""
    try:
        result = fn(**args)
    except engine.VideoEditorError as exc:
        logger.warning("video editor tool failed: %s", exc)
        result = {"ok": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


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
    ctx.register_tool(name="video_editor_captions", toolset="video_editor", schema=CAPTIONS_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.captions, args), check_fn=engine.runtime_ready,
                      description="Generate output-timeline caption drafts.", emoji="🔤")
    ctx.register_tool(name="video_editor_caption_approve", toolset="video_editor", schema=CAPTION_APPROVE_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.caption_approve, args), check_fn=engine.runtime_ready,
                      description="Correct and approve captions before final rendering.", emoji="✅")
    ctx.register_tool(name="video_editor_cards", toolset="video_editor", schema=CARDS_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.cards, args), check_fn=engine.runtime_ready,
                      description="Add timed editorial cards.", emoji="🃏")
    ctx.register_tool(name="video_editor_capture", toolset="video_editor", schema=CAPTURE_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.capture_page, args), check_fn=engine.runtime_ready,
                      description="Capture a real public page as safe proof B-roll.", emoji="🌐")
    ctx.register_tool(name="video_editor_proof", toolset="video_editor", schema=PROOF_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.proof, args), check_fn=engine.runtime_ready,
                      description="Place real-page proof B-roll on the timeline.", emoji="🔎")
    ctx.register_tool(name="video_editor_sound", toolset="video_editor", schema=SOUND_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.sound, args), check_fn=engine.runtime_ready,
                      description="Plan and tune sound effects.", emoji="🔊")
    ctx.register_tool(name="video_editor_look", toolset="video_editor", schema=LOOK_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.look, args), check_fn=engine.runtime_ready,
                      description="Preview or apply lighting and colour correction.", emoji="💡")
    ctx.register_tool(name="video_editor_master", toolset="video_editor", schema=MASTER_SCHEMA,
                      handler=lambda args, **_: _guard(enrichment.master, args), check_fn=engine.runtime_ready,
                      description="Render the final enriched video package.", emoji="🎞️")
    logger.info("Shared video editor registered (runtime_ready=%s)", engine.runtime_ready())
