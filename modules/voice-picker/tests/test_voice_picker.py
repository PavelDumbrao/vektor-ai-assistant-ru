from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml


PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "__init__.py"
spec = importlib.util.spec_from_file_location(
    "voice_picker_under_test",
    PLUGIN,
    submodule_search_locations=[str(PLUGIN.parent)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)
_ORIGINAL_GET_HOME = module._get_home
_ORIGINAL_SAMPLES_DIR = module.SAMPLES_DIR


def _make_source(
    user_id: str = "12345",
    chat_id: str = "12345",
    platform: str = "telegram",
    chat_type: str = "dm",
):
    plat = SimpleNamespace(value=platform) if platform else None
    return SimpleNamespace(
        platform=plat,
        chat_id=chat_id,
        user_id=user_id,
        chat_type=chat_type,
    )


def _make_event(text: str, user_id: str = "12345", chat_id: str = "12345", chat_type: str = "dm"):
    return SimpleNamespace(
        text=text,
        message_id="101",
        source=_make_source(user_id=user_id, chat_id=chat_id, chat_type=chat_type),
    )


class VoicePickerChoiceParsingTests(unittest.TestCase):
    def test_all_ten_voice_labels_parse_correctly(self):
        for label, voice_id, gender in module.VOICES:
            match = module._parse_voice_choice(label)
            self.assertIsNotNone(match, f"Failed to parse label: {label}")
            self.assertEqual(match[0], label)
            self.assertEqual(match[1], voice_id)
            self.assertEqual(match[2], gender)

    def test_case_insensitive_label_parsing(self):
        label = "♀ Марина — тёплый рассказчик"
        match = module._parse_voice_choice(label.lower())
        self.assertIsNotNone(match)
        self.assertEqual(match[1], "ymDCYd8puC7gYjxIamPt")

    def test_digits_one_to_ten_parse_correctly(self):
        for i in range(1, 11):
            match = module._parse_voice_choice(str(i))
            self.assertIsNotNone(match, f"Failed to parse digit: {i}")
            self.assertEqual(match, module.VOICES[i - 1])

            match_dot = module._parse_voice_choice(f"{i}.")
            self.assertIsNotNone(match_dot, f"Failed to parse digit with dot: {i}.")
            self.assertEqual(match_dot, module.VOICES[i - 1])

    def test_invalid_and_garbage_inputs_return_none(self):
        garbage = [
            "",
            "   ",
            "0",
            "11",
            "-1",
            "42",
            "random garbage text",
            "Марина",
            "1 2",
            "voice_pick",
            "ymDCYd8puC7gYjxIamPt",
        ]
        for item in garbage:
            self.assertIsNone(
                module._parse_voice_choice(item),
                f"Expected None for input: {item!r}",
            )

    def test_extract_voice_name(self):
        self.assertEqual(module._extract_voice_name("♂ Артём — подкаст"), "Артём")
        self.assertEqual(module._extract_voice_name("♀ Марина — тёплый рассказчик"), "Марина")
        self.assertEqual(module._extract_voice_name("♀ Sarah — уверенная (EN)"), "Sarah")
        self.assertEqual(module._extract_voice_name("♀ Lily — бархатная (EN)"), "Lily")


class VoicePickerConfigAtomicTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        module._get_home = lambda: self.home

    def tearDown(self):
        module._get_home = _ORIGINAL_GET_HOME
        self.temp_dir.cleanup()

    def test_atomic_config_update_preserves_other_keys_and_permissions(self):
        config_path = self.home / "config.yaml"
        initial_config = {
            "agent": {"name": "TestBot", "temperature": 0.7},
            "model": "claude-3-5-sonnet",
            "tts": {
                "provider": "elevenlabs",
                "elevenlabs": {
                    "api_key": "some-secret-key",
                    "voice_id": "old_voice",
                    "model_id": "old_model",
                    "stability": 0.5,
                },
            },
            "platforms": {
                "telegram": {
                    "extra": {"business_owner_ids": [12345]},
                }
            },
            "custom_setting": [1, 2, 3],
        }
        config_path.write_text(yaml.safe_dump(initial_config, sort_keys=False), encoding="utf-8")
        os.chmod(config_path, 0o600)

        new_voice_id = "ymDCYd8puC7gYjxIamPt"
        module._update_config_voice(new_voice_id)

        st = config_path.stat()
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

        updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["tts"]["elevenlabs"]["voice_id"], new_voice_id)
        self.assertEqual(updated["tts"]["elevenlabs"]["model_id"], "eleven_v4_turbo")

        self.assertEqual(updated["agent"], initial_config["agent"])
        self.assertEqual(updated["model"], "claude-3-5-sonnet")
        self.assertEqual(updated["tts"]["provider"], "elevenlabs")
        self.assertEqual(updated["tts"]["elevenlabs"]["api_key"], "some-secret-key")
        self.assertEqual(updated["tts"]["elevenlabs"]["stability"], 0.5)
        self.assertEqual(updated["platforms"], initial_config["platforms"])
        self.assertEqual(updated["custom_setting"], [1, 2, 3])

    def test_atomic_config_rejects_symlink(self):
        target = self.home / "real_config.yaml"
        target.write_text("{}", encoding="utf-8")
        sym = self.home / "config.yaml"
        sym.symlink_to(target)

        with self.assertRaises(RuntimeError):
            module._update_config_voice("rQOBu7YxCDxGiFdTm28w")


class VoicePickerStateAndTTLTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        module._get_home = lambda: self.home
        with module._state_lock:
            module._pending_memory.clear()

    def tearDown(self):
        module._get_home = _ORIGINAL_GET_HOME
        self.temp_dir.cleanup()

    def test_state_persistence_file_mode_and_content(self):
        user_id = "998877"
        now = time.time()
        module._set_pending(user_id, now=now)

        state_file = module._state_file_path()
        self.assertTrue(state_file.is_file())
        self.assertEqual(stat.S_IMODE(state_file.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)

        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn(user_id, data)
        self.assertAlmostEqual(data[user_id], now + module.PENDING_TTL_SECONDS, delta=1.0)

    def test_ttl_valid_and_expiration(self):
        user_id = "112233"
        base_time = 1000000.0

        module._set_pending(user_id, now=base_time)

        self.assertTrue(module._is_pending(user_id, now=base_time + 540))
        self.assertFalse(module._is_pending(user_id, now=base_time + 601))

        with module._state_lock:
            self.assertNotIn(user_id, module._pending_memory)

    def test_repeated_set_pending_resets_ttl(self):
        user_id = "555"
        base_time = 1000000.0

        module._set_pending(user_id, now=base_time)
        module._set_pending(user_id, now=base_time + 480)

        self.assertTrue(module._is_pending(user_id, now=base_time + 700))

    def test_clear_pending_removes_from_memory_and_disk(self):
        user_id = "444"
        module._set_pending(user_id)
        self.assertTrue(module._is_pending(user_id))

        module._clear_pending(user_id)
        self.assertFalse(module._is_pending(user_id))
        data = json.loads(module._state_file_path().read_text(encoding="utf-8"))
        self.assertNotIn(user_id, data)


class VoicePickerSecurityAndAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        module._get_home = lambda: self.home
        with module._state_lock:
            module._pending_memory.clear()

        config = {
            "platforms": {
                "telegram": {
                    "extra": {
                        "business_owner_ids": [1001, "1002"],
                        "allow_admin_from": ["2001"],
                    },
                    "allow_admin_from": [2002],
                }
            },
            "allow_admin_from": [3001],
        }
        (self.home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        os.chmod(self.home / "config.yaml", 0o600)

    def tearDown(self):
        module._get_home = _ORIGINAL_GET_HOME
        self.temp_dir.cleanup()

    def test_authorized_users_recognized(self):
        for uid in ("1001", "1002", "2001", "2002", "3001"):
            source = _make_source(user_id=uid, chat_id=uid, platform="telegram", chat_type="dm")
            self.assertTrue(module._is_authorized(source), f"User {uid} should be authorized")

    def test_unauthorized_user_rejected(self):
        source = _make_source(user_id="9999", chat_id="9999", platform="telegram", chat_type="dm")
        self.assertFalse(module._is_authorized(source))

    def test_non_telegram_platform_rejected(self):
        source = _make_source(user_id="1001", chat_id="1001", platform="discord", chat_type="dm")
        self.assertFalse(module._is_authorized(source))

    def test_group_chat_rejected(self):
        source = _make_source(user_id="1001", chat_id="-10099999", platform="telegram", chat_type="group")
        self.assertFalse(module._is_authorized(source))

    def test_stranger_cannot_trigger_golos_or_intercept_selection(self):
        gateway = SimpleNamespace()
        stranger_event = _make_event("/golos", user_id="9999", chat_id="9999")
        result = module._on_pre_gateway_dispatch(stranger_event, gateway)
        self.assertIsNone(result)
        self.assertFalse(module._is_pending("9999"))

        module._set_pending("1001")
        self.assertTrue(module._is_pending("1001"))

        stranger_choice = _make_event("♀ Марина — тёплый рассказчик", user_id="9999", chat_id="9999")
        result = module._on_pre_gateway_dispatch(stranger_choice, gateway)
        self.assertIsNone(result)

        self.assertTrue(module._is_pending("1001"))


class VoicePickerPreGatewayDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        module._get_home = lambda: self.home
        module.SAMPLE_PAUSE_SECONDS = 0.0
        # Real samples live in /opt/vektor/voice-samples on the VPS; CI has none, so fake all 10 here.
        self.samples_dir = self.home / "voice-samples"
        self.samples_dir.mkdir()
        for _label, voice_id, _gender in module.VOICES:
            (self.samples_dir / f"{voice_id}.ogg").write_bytes(b"dummy_opus")
        module.SAMPLES_DIR = self.samples_dir
        with module._state_lock:
            module._pending_memory.clear()
        module._tasks.clear()

        self.initial_config = {
            "tts": {
                "elevenlabs": {
                    "voice_id": "initial_voice",
                    "model_id": "initial_model",
                }
            },
            "platforms": {
                "telegram": {
                    "extra": {
                        "business_owner_ids": [1001],
                    }
                }
            },
        }
        (self.home / "config.yaml").write_text(yaml.safe_dump(self.initial_config), encoding="utf-8")
        os.chmod(self.home / "config.yaml", 0o600)

    def tearDown(self):
        module._get_home = _ORIGINAL_GET_HOME
        module.SAMPLES_DIR = _ORIGINAL_SAMPLES_DIR
        module.SAMPLE_PAUSE_SECONDS = 0.3
        self.temp_dir.cleanup()

    async def test_golos_command_prompts_and_skips(self):
        mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        event = _make_event("/golos", user_id="1001", chat_id="1001")
        result = module._on_pre_gateway_dispatch(event, gateway)
        self.assertEqual(result, {"action": "skip", "reason": "voice-picker-prompt"})

        await asyncio.gather(*list(module._tasks))
        self.assertTrue(module._is_pending("1001"))

        # Verify 10 send_voice calls in exact order 1..10
        self.assertEqual(mock_bot.send_voice.await_count, 10)
        for i, call in enumerate(mock_bot.send_voice.await_args_list, start=1):
            kwargs = call.kwargs
            expected_label = module.VOICES[i - 1][0]
            self.assertEqual(kwargs["chat_id"], "1001")
            self.assertEqual(kwargs["caption"], f"{i}. {expected_label}")

        # Verify keyboard prompt message
        mock_bot.send_message.assert_awaited_once()
        _, kwargs = mock_bot.send_message.call_args
        self.assertEqual(kwargs.get("chat_id"), "1001")
        self.assertEqual(kwargs.get("text"), "Послушай примеры выше и выбери голос кнопкой 👇")
        reply_markup = kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        self.assertEqual(len(reply_markup.keyboard), 5)
        self.assertEqual(len(reply_markup.keyboard[0]), 2)

    async def test_voice_pick_alias_works(self):
        mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        event = _make_event("/voice_pick", user_id="1001", chat_id="1001")
        result = module._on_pre_gateway_dispatch(event, gateway)
        self.assertEqual(result, {"action": "skip", "reason": "voice-picker-prompt"})

        await asyncio.gather(*list(module._tasks))
        self.assertTrue(module._is_pending("1001"))
        self.assertEqual(mock_bot.send_voice.await_count, 10)
        mock_bot.send_message.assert_awaited_once()

    async def test_golos_missing_sample_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as sample_dir:
            temp_samples = Path(sample_dir)
            module.SAMPLES_DIR = temp_samples

            # Create only 3 of the 10 samples
            for _, voice_id, _ in module.VOICES[:3]:
                (temp_samples / f"{voice_id}.ogg").write_bytes(b"dummy_opus")

            mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
            mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
            gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

            event = _make_event("/golos", user_id="1001", chat_id="1001")
            result = module._on_pre_gateway_dispatch(event, gateway)
            self.assertEqual(result, {"action": "skip", "reason": "voice-picker-prompt"})

            await asyncio.gather(*list(module._tasks))
            # Exactly 3 send_voice calls
            self.assertEqual(mock_bot.send_voice.await_count, 3)
            # Reply keyboard is still sent
            mock_bot.send_message.assert_awaited_once()

    async def test_golos_sample_send_error_is_logged_and_continues(self):
        mock_bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_voice=AsyncMock(side_effect=[
                None,  # 1 ok
                RuntimeError("telegram timeout"),  # 2 fails
                None, None, None, None, None, None, None, None  # 3..10 ok
            ]),
        )
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        with self.assertLogs("voice_picker_under_test", level="WARNING") as log_capture:
            event = _make_event("/golos", user_id="1001", chat_id="1001")
            result = module._on_pre_gateway_dispatch(event, gateway)
            self.assertEqual(result, {"action": "skip", "reason": "voice-picker-prompt"})

            await asyncio.gather(*list(module._tasks))

        # Check log contains "sample send failed" without user_id
        log_text = "\n".join(log_capture.output)
        self.assertIn("sample send failed", log_text)
        self.assertNotIn("1001", log_text)

        # All 10 attempts were made
        self.assertEqual(mock_bot.send_voice.await_count, 10)
        # Reply keyboard still sent
        mock_bot.send_message.assert_awaited_once()

    async def test_selection_by_button_label_updates_config_and_rewrites_voice_on(self):
        mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        module._set_pending("1001")

        with self.assertLogs("voice_picker_under_test", level="INFO") as log_capture:
            event = _make_event("♀ Марина — тёплый рассказчик", user_id="1001", chat_id="1001")
            result = module._on_pre_gateway_dispatch(event, gateway)
            # Rewrite contract: returns rewrite to /voice on
            self.assertEqual(result, {"action": "rewrite", "text": "/voice on"})

            await asyncio.gather(*list(module._tasks))

        log_text = "\n".join(log_capture.output)
        self.assertIn("voice changed", log_text)
        self.assertNotIn("1001", log_text)

        self.assertFalse(module._is_pending("1001"))

        conf = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(conf["tts"]["elevenlabs"]["voice_id"], "ymDCYd8puC7gYjxIamPt")
        self.assertEqual(conf["tts"]["elevenlabs"]["model_id"], "eleven_v4_turbo")

        # Check confirmation sent via send_voice with sample of chosen voice and ReplyKeyboardRemove
        mock_bot.send_voice.assert_awaited_once()
        kwargs = mock_bot.send_voice.call_args.kwargs
        expected_caption = (
            "Готово! Теперь я говорю голосом «Марина». "
            "Голосовые ответы включены: на голосовое отвечу голосом. "
            "Все ответы голосом — /voice tts, выключить — /voice off, сменить голос — /golos"
        )
        self.assertEqual(kwargs["caption"], expected_caption)
        self.assertEqual(kwargs["chat_id"], "1001")
        reply_markup = kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        self.assertTrue(getattr(reply_markup, "remove_keyboard", False))

    async def test_selection_by_digit_rewrites_voice_on(self):
        mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        module._set_pending("1001")

        event = _make_event("1", user_id="1001", chat_id="1001")
        result = module._on_pre_gateway_dispatch(event, gateway)
        self.assertEqual(result, {"action": "rewrite", "text": "/voice on"})

        await asyncio.gather(*list(module._tasks))

        conf = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(conf["tts"]["elevenlabs"]["voice_id"], "rQOBu7YxCDxGiFdTm28w")
        self.assertEqual(conf["tts"]["elevenlabs"]["model_id"], "eleven_v4_turbo")

        mock_bot.send_voice.assert_awaited_once()
        kwargs = mock_bot.send_voice.call_args.kwargs
        self.assertIn("Готово! Теперь я говорю голосом «Артём».", kwargs["caption"])

    async def test_selection_fallback_to_send_message_when_sample_file_missing(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            module.SAMPLES_DIR = Path(empty_dir)

            mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
            mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
            gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

            module._set_pending("1001")

            event = _make_event("1", user_id="1001", chat_id="1001")
            result = module._on_pre_gateway_dispatch(event, gateway)
            self.assertEqual(result, {"action": "rewrite", "text": "/voice on"})

            await asyncio.gather(*list(module._tasks))

            # Since sample file is missing, send_voice is skipped, send_message is used
            mock_bot.send_voice.assert_not_awaited()
            mock_bot.send_message.assert_awaited_once()
            _, kwargs = mock_bot.send_message.call_args
            self.assertIn("Готово! Теперь я говорю голосом «Артём».", kwargs["text"])
            self.assertTrue(getattr(kwargs.get("reply_markup"), "remove_keyboard", False))

    async def test_selection_fallback_when_send_voice_raises(self):
        mock_bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_voice=AsyncMock(side_effect=RuntimeError("telegram error")),
        )
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        module._set_pending("1001")

        with self.assertLogs("voice_picker_under_test", level="WARNING") as log_capture:
            event = _make_event("1", user_id="1001", chat_id="1001")
            result = module._on_pre_gateway_dispatch(event, gateway)
            self.assertEqual(result, {"action": "rewrite", "text": "/voice on"})

            await asyncio.gather(*list(module._tasks))

        log_text = "\n".join(log_capture.output)
        self.assertIn("sample send failed", log_text)

        mock_bot.send_voice.assert_awaited_once()
        mock_bot.send_message.assert_awaited_once()
        self.assertIn("Готово! Теперь я говорю голосом «Артём».", mock_bot.send_message.call_args.kwargs["text"])

    async def test_unrelated_text_is_not_intercepted_and_passed_to_llm(self):
        mock_bot = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
        mock_adapter = SimpleNamespace(_bot=mock_bot, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        module._set_pending("1001")

        event = _make_event("Расскажи шутку про программистов", user_id="1001", chat_id="1001")
        result = module._on_pre_gateway_dispatch(event, gateway)

        self.assertIsNone(result)
        self.assertTrue(module._is_pending("1001"))
        mock_bot.send_message.assert_not_awaited()
        mock_bot.send_voice.assert_not_awaited()

    async def test_fallback_to_adapter_send_when_bot_missing(self):
        mock_adapter = SimpleNamespace(_bot=None, _app=None, send=AsyncMock())
        gateway = SimpleNamespace(_adapter_for_source=lambda src: mock_adapter)

        event = _make_event("/golos", user_id="1001", chat_id="1001")
        result = module._on_pre_gateway_dispatch(event, gateway)
        self.assertEqual(result, {"action": "skip", "reason": "voice-picker-prompt"})

        await asyncio.gather(*list(module._tasks))
        mock_adapter.send.assert_awaited_once()
        self.assertIn("Выбери голос ассистента:", mock_adapter.send.call_args[0][1])
        self.assertIn("1. ♂ Артём — подкаст", mock_adapter.send.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
