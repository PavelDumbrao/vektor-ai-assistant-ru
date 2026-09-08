"""The same evidence-backed intake after Hermes' existing speech recognition."""
from __future__ import annotations

import copy
import re

from . import intake


def process(args, source):
    if set(args) - {"items", "ambiguities", "dry_run"}:
        raise ValueError("invalid_voice_intake_fields")
    questions = args.get("ambiguities", [])
    if not isinstance(questions, list) or len(questions) > 5 or any(not isinstance(q, str) or not 3 <= len(q) <= 300 for q in questions):
        raise ValueError("invalid_voice_clarifications")
    # Do not silently choose AM/PM for a bare spoken hour.
    text = source["text"].casefold()
    bare_hour = re.search(r"\bв\s+(?:[1-9]|1[01])\b(?!:)(?!\s+(?:пункт|част|шаг|раз|строк))", text)
    if bare_hour and not re.search(r"утра|вечера|ночи|дня|\d{1,2}:\d{2}", text):
        questions = questions or ["Это время утром или вечером?"]
    if questions:
        return {"ok": True, "needs_clarification": True, "question": questions[0],
                "remaining_questions": len(questions) - 1, "stored": False}
    result = intake.capture({k: args[k] for k in ("items", "dry_run") if k in args}, source)
    return {**result, "needs_clarification": False, "input": "current_owner_transcribed_or_typed_message",
            "instruction": "Покажи коротко выделенные задачи/идеи. Это кандидаты, не выполненные дела. При сомнении в распознавании спроси владельца."}


SCHEMA = copy.deepcopy(intake.CAPTURE_SCHEMA)
SCHEMA["name"] = "assistant_voice_intake"
SCHEMA["description"] = "Преобразует уже распознанное голосовое Павла в задачи/идеи с цитатами. Использует штатный STT Hermes; не загружает аудио сторонним сервисам заново. При неоднозначности задаёт один вопрос и ничего не сохраняет. Есть dry_run."
SCHEMA["parameters"]["properties"].pop("source_ref")
SCHEMA["parameters"]["required"] = []
SCHEMA["parameters"]["properties"]["items"]["minItems"] = 0
SCHEMA["parameters"]["properties"]["ambiguities"] = {"type": "array", "maxItems": 5, "items": {"type": "string"}, "description": "Неясные сроки, имена, отрицания или сомнения STT. При наличии запись не выполняется."}
