pytest_plugins = ["test_focus_assistant"]


def test_transcribed_voice_uses_same_candidate_contract(plugin):
    source = {"kind": "owner", "source_ref": "owner:voice:1", "text": "Запиши идею: сделать короткий урок. И подготовить план 2026-09-10."}
    result = plugin.voice.process({"items": [{"title": "Сделать короткий урок", "quote": "сделать короткий урок", "kind": "idea"},
        {"title": "Подготовить план", "quote": "подготовить план 2026-09-10", "kind": "task", "due_at": "2026-09-10", "due_quote": "2026-09-10"}]}, source)
    assert result["needs_clarification"] is False
    assert len(result["results"]) == 2
    assert all(row["item"]["state"] == "candidate" for row in result["results"])


def test_spoken_ambiguous_hour_is_not_guessed(plugin):
    source = {"kind": "owner", "source_ref": "owner:voice:2", "text": "Напомни завтра в 7 позвонить."}
    result = plugin.voice.process({"items": [{"title": "Позвонить", "quote": source["text"]}]}, source)
    assert result["needs_clarification"] is True
    assert result["stored"] is False
    assert plugin.ledger.list_items({})["count"] == 0


def test_stt_uncertainty_asks_only_one_question(plugin):
    source = {"kind": "owner", "source_ref": "owner:voice:3", "text": "Отправить кому-то файл."}
    result = plugin.voice.process({"items": [], "ambiguities": ["Кому отправить файл?", "Какой файл нужен?"]}, source)
    assert result["question"] == "Кому отправить файл?"
    assert result["remaining_questions"] == 1
    assert result["stored"] is False
    assert "items" not in plugin.voice.SCHEMA["parameters"]["required"]


def test_successful_gateway_transcript_is_used_not_raw_voice_placeholder(plugin):
    gate = plugin.OwnerGate("1")
    transcript = "Запиши идею: короткий урок."
    gate.observe(session_id="s", turn_id="t", sender_id="1", platform="telegram", chat_type="dm",
                 raw_user_message="[Voice message]", user_message=transcript, is_internal_event=False)
    source = gate.source("s", "current_voice")
    result = plugin.voice.process({"items": [{"title": "Короткий урок", "quote": "короткий урок", "kind": "idea"}], "dry_run": True}, source)
    assert result["ok"] is True and result["stored"] is False


def test_hermes_021_quoted_transcript_before_caption(plugin):
    gate = plugin.OwnerGate("1")
    caption = "Тестовая запись. Только dry_run."
    text = '"Тестовая идея – записать короткое видео о работе личного ассистента."\n\n' + caption
    gate.observe(session_id="s", turn_id="v", sender_id="1", platform="telegram", chat_type="dm",
                 raw_user_message=caption, user_message=text, is_internal_event=False)
    result = plugin.voice.process({"dry_run": True, "items": [{"title": "Короткое видео", "kind": "idea",
        "quote": "записать короткое видео о работе личного ассистента"}]}, gate.source("s", "current_voice"))
    assert result["ok"] and result["stored"] is False
    assert len(result["candidates"]) == 1
