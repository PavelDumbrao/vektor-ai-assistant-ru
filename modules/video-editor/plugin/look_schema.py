LOOK_SCHEMA = {
    "name": "video_editor_look",
    "description": "Preview or apply lighting correction and colour grade to the verified cut without overwriting the original. Preview first, inspect the comparison, then apply.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "preset": {"type": "string", "enum": ["none", "warm_lift", "neutral_punch", "cool_clean"], "default": "neutral_punch"},
            "strength": {"type": "string", "enum": ["subtle", "normal", "strong"], "default": "normal"},
            "apply": {"type": "boolean", "default": False},
            "at": {"type": "number", "minimum": 0, "description": "Optional comparison frame time in output seconds."},
        },
        "required": ["job_id"],
    },
}
