CAPTIONS_SCHEMA = {
    "name": "video_editor_captions",
    "description": "Generate output-timeline caption chunks after the base cut is verified. Returns a draft that must be read and approved before master rendering.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            "fixes": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "required": ["job_id"],
    },
}

CAPTION_APPROVE_SCHEMA = {
    "name": "video_editor_caption_approve",
    "description": "Apply explicit caption corrections and mark the captions proofread. Keep every correction at 4 words or fewer and never merge neighboring chunks; timing belongs to the existing chunk. Call only after reading every caption chunk.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "corrections": {"type": "array", "maxItems": 100, "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0},
                    "text": {"type": "string", "maxLength": 220},
                    "emphasis": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                },
                "required": ["index", "text"],
            }},
        },
        "required": ["job_id"],
    },
}

CARDS_SCHEMA = {
    "name": "video_editor_cards",
    "description": "Write timed editorial cards on the output timeline. Use cards to keep the frame changing and to explain beats, not as fake proof of real products/pages.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "cards": {"type": "array", "maxItems": 30, "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "duration": {"type": "number", "minimum": 0.3},
                    "style": {"type": "string", "enum": ["split", "band", "full"]},
                    "big": {"type": "string", "maxLength": 140},
                    "kicker": {"type": "string", "maxLength": 80},
                    "sub": {"type": "string", "maxLength": 180},
                    "brand_colour": {"type": "string"},
                    "items": {"type": "array", "maxItems": 6, "items": {"type": "object"}},
                },
                "required": ["start", "duration", "big"],
            }},
        },
        "required": ["job_id", "cards"],
    },
}

CAPTURE_SCHEMA = {
    "name": "video_editor_capture",
    "description": "Capture a real public HTTPS page as proof B-roll using a hardened browser that blocks private/local network destinations.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "url": {"type": "string"},
            "name": {"type": "string", "description": "Short asset id, letters/numbers/_/- only."},
            "find": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        },
        "required": ["job_id", "url", "name"],
    },
}

PROOF_SCHEMA = {
    "name": "video_editor_proof",
    "description": "Place captured real-page proof assets on the output timeline as zoom or scroll beats.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "beats": {"type": "array", "maxItems": 30, "items": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string"},
                    "start": {"type": "number", "minimum": 0},
                    "duration": {"type": "number", "minimum": 0.4},
                    "action": {"type": "string", "enum": ["zoom", "scroll"]},
                    "target": {"type": "string"},
                    "from_y": {"type": "number", "minimum": 0},
                    "to_y": {"type": "number", "minimum": 0},
                    "highlight": {"type": "boolean"},
                    "highlight_at": {"type": "number", "minimum": 0},
                },
                "required": ["asset", "start", "duration", "action"],
            }},
        },
        "required": ["job_id", "beats"],
    },
}

SOUND_SCHEMA = {
    "name": "video_editor_sound",
    "description": "Plan and peak-align SFX from the shared licensed library. gain_scale lets the agent correct audibility after the sound verification gate.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "gain_scale": {"type": "number", "minimum": 0.5, "maximum": 1.5, "default": 1.0},
        },
        "required": ["job_id"],
    },
}

MASTER_SCHEMA = {
    "name": "video_editor_master",
    "description": "Run the final quality gates, offline-pinned HyperFrames render, SFX audibility check, optional music ducking, variants and thumbnail package.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "music_path": {"type": "string", "description": "Optional profile-owned audio file from workspace or Hermes audio cache."},
            "music_level": {"type": "number", "minimum": 0.02, "maximum": 0.35, "default": 0.12},
            "aspects": {"type": "array", "items": {"type": "string", "enum": ["9:16", "1:1", "16:9"]}, "maxItems": 3},
        },
        "required": ["job_id"],
    },
}
