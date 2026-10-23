"""Tests for Hermes Forge support module."""

import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MANAGER_DIR = Path(__file__).resolve().parents[1]
if str(MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(MANAGER_DIR))

import support
from support import (
    PAVEL_ID,
    _build_system_prompt,
    _llm_answer,
    _load_faq,
    _select_faq_context,
    admin_reply,
    escalate,
    expire,
    handle_callback,
    handle_message,
    start,
)


class MockSender:
    """Helper mock to capture send calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, chat_id: int, text: str, markup: dict | None = None):
        self.calls.append({'chat_id': chat_id, 'text': text, 'markup': markup})
        return {'ok': True, 'result': {'message_id': len(self.calls)}}

    @property
    def texts(self) -> list[str]:
        return [c['text'] for c in self.calls]


def test_start_creates_session_and_sends_greeting():
    state: dict = {}
    sender = MockSender()

    start(1001, 2001, state, sender, locale='ru')

    assert 'support' in state
    assert '2001' in state['support']
    session = state['support']['2001']

    assert session['active'] is True
    assert isinstance(session['started_at'], int)
    assert isinstance(session['last_at'], int)
    assert session['turns'] == []
    assert session['count_today'] == 0
    assert isinstance(session['day'], str)
    assert session['chat_id'] == 1001
    assert session['locale'] == 'ru'

    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call['chat_id'] == 1001
    assert "Hermes Forge" in call['text']

    markup = call['markup']
    assert markup and 'inline_keyboard' in markup
    buttons = markup['inline_keyboard'][0]
    assert len(buttons) == 2
    callbacks = [b['callback_data'] for b in buttons]
    assert 'support_human' in callbacks
    assert 'support_close' in callbacks


def test_start_english_locale():
    state: dict = {}
    sender = MockSender()

    start(1001, 2001, state, sender, locale='en')
    session = state['support']['2001']
    assert session['locale'] == 'en'

    call = sender.calls[0]
    assert "Hello!" in call['text']
    buttons = call['markup']['inline_keyboard'][0]
    texts = [b['text'] for b in buttons]
    assert any("human" in t.lower() for t in texts)
    assert any("close" in t.lower() for t in texts)


def test_handle_message_without_session():
    sender = MockSender()
    state: dict = {}

    # No session
    assert handle_message(1001, 2001, "Привет", state, sender) is False
    assert len(sender.calls) == 0

    # Inactive session
    state = {'support': {'2001': {'active': False}}}
    assert handle_message(1001, 2001, "Привет", state, sender) is False
    assert len(sender.calls) == 0


def test_handle_message_with_session_calls_llm_and_sends_answer(monkeypatch):
    state: dict = {}
    sender = MockSender()
    start(1001, 2001, state, sender, locale='ru')
    sender.calls.clear()

    llm_mock = MagicMock(return_value="Для начала отправьте команду /start вашему боту.")
    monkeypatch.setattr(support, '_llm_answer', llm_mock)

    result = handle_message(1001, 2001, "Как начать работу?", state, sender)

    assert result is True
    assert llm_mock.call_count == 1
    messages, _settings = llm_mock.call_args[0]

    assert messages[0]['role'] == 'system'
    system_text = messages[0]['content']
    assert "Hermes Forge" in system_text
    # System prompt contains FAQ fragment
    assert "Как начать" in system_text or "FAQ" in system_text

    assert messages[-1]['role'] == 'user'
    assert messages[-1]['content'] == "Как начать работу?"

    assert len(sender.calls) == 1
    assert "отправьте команду /start" in sender.calls[0]['text']

    session = state['support']['2001']
    assert len(session['turns']) == 2
    assert session['turns'][0] == {'role': 'user', 'content': "Как начать работу?"}
    assert session['turns'][1] == {'role': 'assistant', 'content': "Для начала отправьте команду /start вашему боту."}
    assert session['count_today'] == 1


def test_history_grows_and_trims_to_8_turns(monkeypatch):
    state: dict = {}
    sender = MockSender()
    start(1001, 2001, state, sender, locale='ru')

    monkeypatch.setattr(support, '_llm_answer', lambda msgs, settings: "Ответ: " + ("x" * 2000))

    for i in range(6):
        long_query = f"Вопрос {i}: " + ("y" * 2000)
        res = handle_message(1001, 2001, long_query, state, sender)
        assert res is True

    session = state['support']['2001']
    # 6 exchanges * 2 = 12 turns, but capped at 8
    assert len(session['turns']) == 8
    for turn in session['turns']:
        assert len(turn['content']) <= 1500


def test_daily_limit_30_messages_offers_human_without_llm(monkeypatch):
    state: dict = {}
    sender = MockSender()
    start(1001, 2001, state, sender, locale='ru')
    sender.calls.clear()

    session = state['support']['2001']
    session['count_today'] = 30

    llm_mock = MagicMock()
    monkeypatch.setattr(support, '_llm_answer', llm_mock)

    res = handle_message(1001, 2001, "Еще вопрос", state, sender)

    assert res is True
    assert llm_mock.call_count == 0  # LLM must NOT be called

    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert "30" in call['text']
    markup = call['markup']
    buttons = markup['inline_keyboard'][0]
    assert any(b['callback_data'] == 'support_human' for b in buttons)


def test_llm_error_offers_human(monkeypatch):
    state: dict = {}
    sender = MockSender()
    start(1001, 2001, state, sender, locale='ru')
    sender.calls.clear()

    monkeypatch.setattr(support, '_llm_answer', MagicMock(side_effect=RuntimeError("LLM timeout")))

    res = handle_message(1001, 2001, "Сложный вопрос", state, sender)

    assert res is True
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert "Не получилось ответить автоматически, передаю человеку?" in call['text']
    markup = call['markup']
    buttons = markup['inline_keyboard'][0]
    assert any(b['callback_data'] == 'support_human' for b in buttons)


def test_support_human_escalates_to_admin_with_user_label_and_turns():
    now = int(time.time())
    state = {
        'support': {
            '2001': {
                'active': True,
                'started_at': now - 300,
                'last_at': now - 10,
                'turns': [
                    {'role': 'user', 'content': 'Привет, бот завис'},
                    {'role': 'assistant', 'content': 'Попробуйте перезапуск в Панели'},
                    {'role': 'user', 'content': 'Не помогло, помогите!'},
                ],
                'count_today': 2,
                'day': time.strftime('%Y-%m-%d', time.gmtime(now)),
                'chat_id': 1001,
                'locale': 'ru',
            }
        }
    }
    sender = MockSender()

    res = handle_callback(1001, 2001, 'support_human', state, sender, user_label='@alice — Алиса')

    assert res is True
    assert len(sender.calls) == 2

    # Check admin message
    admin_call = next(c for c in sender.calls if c['chat_id'] == PAVEL_ID)
    assert "@alice — Алиса" in admin_call['text']
    assert "2001" in admin_call['text']
    assert "Не помогло, помогите!" in admin_call['text']
    assert "/reply 2001" in admin_call['text']

    # Check user message
    user_call = next(c for c in sender.calls if c['chat_id'] == 1001)
    assert "передал" in user_call['text'].lower()
    assert "павлу" in user_call['text'].lower()

    session = state['support']['2001']
    assert session.get('escalated_at') is not None


def test_escalate_throttling_within_one_hour():
    now = int(time.time())
    state = {
        'support': {
            '2001': {
                'active': True,
                'started_at': now - 1000,
                'last_at': now - 10,
                'turns': [{'role': 'user', 'content': 'Помогите'}],
                'count_today': 1,
                'day': time.strftime('%Y-%m-%d', time.gmtime(now)),
                'chat_id': 1001,
                'locale': 'ru',
                'escalated_at': now - 600,  # 10 minutes ago
                'user_label': '@bob (Боб)',
            }
        }
    }
    sender = MockSender()

    escalate(1001, 2001, state, sender, user_label='@bob (Боб)')

    # Admin should NOT receive second message
    admin_calls = [c for c in sender.calls if c['chat_id'] == PAVEL_ID]
    assert len(admin_calls) == 0

    # User receives notification that request was already forwarded
    user_calls = [c for c in sender.calls if c['chat_id'] == 1001]
    assert len(user_calls) == 1
    assert "уже передан" in user_calls[0]['text']


def test_admin_reply_delivers_text_and_resets_escalated_at():
    now = int(time.time())
    state = {
        'support': {
            '2001': {
                'active': True,
                'started_at': now - 1000,
                'last_at': now - 10,
                'turns': [{'role': 'user', 'content': 'Вопрос'}],
                'count_today': 1,
                'day': time.strftime('%Y-%m-%d', time.gmtime(now)),
                'chat_id': 1001,
                'locale': 'ru',
                'escalated_at': now - 300,
            }
        }
    }
    sender = MockSender()

    res = admin_reply(2001, "Привет! Мы перезапустили инстанс, всё работает.", state, sender)

    assert res is True
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call['chat_id'] == 1001
    assert "Мы перезапустили инстанс" in call['text']

    session = state['support']['2001']
    assert session['escalated_at'] is None
    assert len(session['turns']) == 2
    assert "Мы перезапустили инстанс" in session['turns'][-1]['content']


def test_expire_closes_sessions_older_than_30_minutes():
    now = 1700000000
    state = {
        'support': {
            '10': {  # Inactive for 31 minutes -> should expire
                'active': True,
                'started_at': now - 2000,
                'last_at': now - 1860,
            },
            '20': {  # Active 10 minutes ago -> should stay active
                'active': True,
                'started_at': now - 1000,
                'last_at': now - 600,
            },
            '30': {  # Already inactive -> ignored
                'active': False,
                'started_at': now - 5000,
                'last_at': now - 4000,
            },
        }
    }

    expired = expire(state, now=now)

    assert expired == [10]
    assert state['support']['10']['active'] is False
    assert state['support']['20']['active'] is True
    assert state['support']['30']['active'] is False


def test_secret_does_not_leak_into_any_sent_text(monkeypatch):
    secret_key = "sk-super-secret-api-key-9988776655"
    settings = {
        'FORGE_SUPPORT_LLM_API_KEY': secret_key,
        'FORGE_SUPPORT_LLM_BASE_URL': 'https://api.example.com/v1',
        'FORGE_SUPPORT_LLM_MODEL': 'glm-5.3',
    }

    sender = MockSender()
    state: dict = {}

    start(1001, 2001, state, sender, locale='ru')

    monkeypatch.setattr(support, '_llm_answer', MagicMock(return_value="Нормальный ответ"))
    handle_message(1001, 2001, "Обычный вопрос", state, sender, settings=settings)

    monkeypatch.setattr(support, '_llm_answer', MagicMock(side_effect=RuntimeError(f"HTTPError with {secret_key}")))
    handle_message(1001, 2001, "Ошибка с ключом", state, sender, settings=settings)

    state['support']['2001']['count_today'] = 30
    handle_message(1001, 2001, "Лимит", state, sender, settings=settings)

    handle_callback(1001, 2001, 'support_human', state, sender, user_label='@test')
    admin_reply(2001, "Ответ админа", state, sender)
    handle_callback(1001, 2001, 'support_close', state, sender)

    # Verify that secret_key never appeared in any sent message
    for sent in sender.calls:
        assert secret_key not in sent['text'], f"Secret leaked into text: {sent['text']}"


def test_callback_support_close():
    state: dict = {}
    sender = MockSender()
    start(1001, 2001, state, sender, locale='ru')
    sender.calls.clear()

    res = handle_callback(1001, 2001, 'support_close', state, sender)

    assert res is True
    assert state['support']['2001']['active'] is False
    assert len(sender.calls) == 1
    assert "завершена" in sender.calls[0]['text'].lower()

    # Unknown callback
    assert handle_callback(1001, 2001, 'unknown_data', state, sender) is False


def test_faq_selection_when_over_budget():
    faq = """
# Введение
Общие сведения.

## 1. Голос и озвучка
Здесь про ElevenLabs и команду /golos.

## 2. Оплата и тариф
Здесь про Продамус и способы оплаты подписки.

## 3. Подключения и Maton
Здесь про подключение Zoom и Maton.
"""
    selected = _select_faq_context(faq, "Как настроить /golos?", max_chars=120)
    assert len(selected) <= 120
    assert "Голос и озвучка" in selected


def test_llm_answer_http_request(monkeypatch):
    class FakeResponse:
        def __init__(self, data: dict):
            self.raw = json.dumps(data).encode('utf-8')

        def read(self):
            return self.raw

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    recorded_req = None

    def fake_urlopen(req, timeout=40):
        nonlocal recorded_req
        recorded_req = req
        return FakeResponse({
            'choices': [
                {'message': {'content': '  Ответ от модели  '}}
            ]
        })

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)

    settings = {
        'FORGE_SUPPORT_LLM_BASE_URL': 'https://custom.llm/v1',
        'FORGE_SUPPORT_LLM_API_KEY': 'my-secret-key',
        'FORGE_SUPPORT_LLM_MODEL': 'custom-model',
    }
    messages = [{'role': 'user', 'content': 'Привет'}]

    ans = _llm_answer(messages, settings)

    assert ans == "Ответ от модели"
    assert recorded_req is not None
    assert recorded_req.full_url == 'https://custom.llm/v1/chat/completions'
    assert recorded_req.headers.get('Authorization') == 'Bearer my-secret-key'
    payload = json.loads(recorded_req.data.decode('utf-8'))
    assert payload['model'] == 'custom-model'
    assert payload['max_tokens'] == 600
    assert payload['temperature'] == 0.2


def test_llm_answer_is_html_escaped(monkeypatch):
    sent = []
    state = {"support": {}}
    support.start(1, 1, state, lambda c, t, m=None: sent.append((c, t, m)))
    monkeypatch.setattr(support, "_llm_answer", lambda messages, settings: "Используйте <b>тег</b> & кнопку")
    support.handle_message(1, 1, "как?", state, lambda c, t, m=None: sent.append((c, t, m)), settings={"FORGE_SUPPORT_LLM_API_KEY": "k"})
    assert "&lt;b&gt;" in sent[-1][1] and "&amp;" in sent[-1][1]
