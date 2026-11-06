"""Support module for Hermes Forge bot manager.

Automated FAQ assistant with LLM + human escalation to Pavel.
State structure: state['support'][str(uid)]
"""

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

PAVEL_ID = 450206471

TEXTS = {
    'ru': {
        'greeting': (
            "Здравствуйте! Это автоматическая поддержка Hermes Forge 🛟\n\n"
            "Задайте любой вопрос по настройке или использованию ассистента, "
            "и я отвечу по базе знаний. При необходимости вы можете позвать человека."
        ),
        'btn_human': "👤 Позвать человека",
        'btn_close': "❌ Завершить",
        'limit_reached': (
            "Вы достигли дневного лимита автоматической поддержки (30 сообщений). "
            "Хотите передать вопрос человеку?"
        ),
        'llm_error': "Не получилось ответить автоматически, передаю человеку?",
        'escalated_user': "Передал ваш запрос Павлу. Специалист скоро свяжется с вами!",
        'already_escalated': "Ваш запрос уже передан Павлу. Пожалуйста, ожидайте ответа.",
        'closed': "Сессия поддержки завершена. Если появятся вопросы — обращайтесь!",
        'admin_reply_prefix': "👨‍💻 <b>Ответ поддержки:</b>",
    },
    'en': {
        'greeting': (
            "Hello! This is Hermes Forge automated support 🛟\n\n"
            "Ask any question about configuring or using your assistant, "
            "and I will reply based on our knowledge base. You can also connect with a human specialist."
        ),
        'btn_human': "👤 Call a human",
        'btn_close': "❌ Close",
        'limit_reached': (
            "You have reached the daily limit of automated support messages (30 messages). "
            "Would you like to connect with a human specialist?"
        ),
        'llm_error': "Couldn't answer automatically, transfer to a human specialist?",
        'escalated_user': "Forwarded your request to Pavel. A specialist will reply shortly!",
        'already_escalated': "Your request has already been forwarded to Pavel. Please wait for a reply.",
        'closed': "Support session ended. If you have any further questions, feel free to reach out!",
        'admin_reply_prefix': "👨‍💻 <b>Support reply:</b>",
    },
}


def _load_faq() -> str:
    faq_path = Path(__file__).resolve().parent / 'docs' / 'client-faq.md'
    if faq_path.is_file():
        return faq_path.read_text(encoding='utf-8')
    return ""


def _select_faq_context(faq_text: str, user_query: str, max_chars: int = 12000) -> str:
    """Select relevant FAQ sections if text exceeds max_chars."""
    if not faq_text or len(faq_text) <= max_chars:
        return faq_text

    sections = re.split(r'\n(?=##\s+)', faq_text)
    if not sections:
        return faq_text[:max_chars]

    intro = ""
    faq_sections = sections
    if not sections[0].strip().startswith('##'):
        intro = sections[0].strip() + "\n\n"
        faq_sections = sections[1:]

    # Extract keywords from query (words with >= 3 characters)
    words = set(re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]{3,}', user_query.lower()))

    scored = []
    for idx, sec in enumerate(faq_sections):
        sec_lower = sec.lower()
        score = sum(1 for w in words if w in sec_lower)
        scored.append((score, idx, sec))

    # Sort descending by score, preserving original order as tiebreaker
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)

    selected = []
    current_len = len(intro)
    for _score, idx, sec in scored:
        sec_len = len(sec) + 2
        if current_len + sec_len <= max_chars:
            selected.append((idx, sec))
            current_len += sec_len
        elif not selected:
            avail = max(0, max_chars - current_len)
            selected.append((idx, sec[:avail]))
            break

    selected.sort(key=lambda item: item[0])
    body = "\n\n".join(sec for _, sec in selected)
    return (intro + body).strip()


def _build_system_prompt(faq_context: str, locale: str = 'ru') -> str:
    lang_rule = "Отвечай по-русски." if locale == 'ru' else f"Отвечай на языке пользователя ({locale})."
    return (
        "Ты — официальный агент автоматической поддержки сервиса Hermes Forge.\n"
        "Твоя задача — помогать клиентам настраивать и использовать их личного AI-ассистента в Telegram.\n\n"
        "ПРАВИЛА И ОГРАНИЧЕНИЯ:\n"
        f"1. {lang_rule} Отвечай кратко, вежливо и понятно.\n"
        "2. Отвечай СТРОГО по приведенной ниже базе знаний (FAQ). Никаких выдумок! Не обещай функций вне FAQ.\n"
        "3. Если информации нет в базе знаний или ты не уверен в ответе, честно признай это и предложи позвать человека кнопкой «Позвать человека».\n"
        "4. НИКОГДА не запрашивай и не принимай пароли, токены, ключи API, данные банковских карт и другие секреты.\n"
        "5. Не раскрывай внутренние пути, имена серверов, токены, секреты и данные других клиентов.\n\n"
        "БАЗА ЗНАНИЙ (FAQ):\n"
        f"{faq_context}\n"
    )


def _resolve_settings(explicit: dict | None, state: dict | None) -> dict:
    if explicit is not None and isinstance(explicit, dict):
        return explicit
    if state and isinstance(state, dict):
        if isinstance(state.get('settings'), dict):
            return state['settings']
        if isinstance(state.get('manager_settings'), dict):
            return state['manager_settings']
    res = {}
    for k in ('FORGE_SUPPORT_LLM_BASE_URL', 'FORGE_SUPPORT_LLM_API_KEY', 'FORGE_SUPPORT_LLM_MODEL'):
        v = os.environ.get(k)
        if v:
            res[k] = v
    return res


def _llm_answer(messages: list[dict], settings: dict | None) -> str:
    """Perform OpenAI-compatible POST {base}/chat/completions."""
    settings = settings or {}
    base_url = (settings.get('FORGE_SUPPORT_LLM_BASE_URL') or 'https://api.openai.com/v1').strip().rstrip('/')
    url = f"{base_url}/chat/completions"
    api_key = str(settings.get('FORGE_SUPPORT_LLM_API_KEY') or '').strip()
    model = str(settings.get('FORGE_SUPPORT_LLM_MODEL') or 'glm-5.3').strip()

    payload = {
        'model': model,
        'messages': messages,
        'max_tokens': 600,
        'temperature': 0.2,
    }

    headers = {
        'Content-Type': 'application/json',
    }
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=req_data, headers=headers, method='POST')

    with urllib.request.urlopen(req, timeout=40) as resp:
        resp_data = json.loads(resp.read().decode('utf-8'))
        choices = resp_data.get('choices')
        if not choices or not isinstance(choices, list):
            raise RuntimeError('invalid_llm_response')
        return choices[0]['message']['content'].strip()


def start(chat_id: int, user_id: int | str, state: dict, send, locale: str = 'ru', user_label: str = '') -> None:
    """Activate support session for user, send greeting with action buttons."""
    uid = str(user_id)
    support_dict = state.setdefault('support', {})
    now = int(time.time())
    today = time.strftime('%Y-%m-%d', time.gmtime(now))

    prev = support_dict.get(uid, {})
    count_today = prev.get('count_today', 0) if prev.get('day') == today else 0

    session = {
        'active': True,
        'started_at': now,
        'last_at': now,
        'turns': [],
        'count_today': count_today,
        'day': today,
        'chat_id': int(chat_id),
        'locale': locale if locale in TEXTS else 'ru',
    }
    if user_label:
        session['user_label'] = user_label
    elif 'user_label' in prev:
        session['user_label'] = prev['user_label']

    support_dict[uid] = session

    loc = session['locale']
    markup = {
        'inline_keyboard': [
            [
                {'text': TEXTS[loc]['btn_human'], 'callback_data': 'support_human'},
                {'text': TEXTS[loc]['btn_close'], 'callback_data': 'support_close'},
            ]
        ]
    }
    send(chat_id, TEXTS[loc]['greeting'], markup)


def handle_message(chat_id: int, user_id: int | str, text: str, state: dict, send, settings: dict | None = None) -> bool:
    """Process user message in active support session."""
    uid = str(user_id)
    support_dict = state.get('support', {})
    if not isinstance(support_dict, dict) or uid not in support_dict:
        return False

    session = support_dict[uid]
    if not isinstance(session, dict) or not session.get('active'):
        return False

    now = int(time.time())
    today = time.strftime('%Y-%m-%d', time.gmtime(now))
    if session.get('day') != today:
        session['day'] = today
        session['count_today'] = 0

    loc = session.get('locale', 'ru')
    loc = loc if loc in TEXTS else 'ru'

    count_today = session.get('count_today', 0)
    if count_today >= 30:
        session['last_at'] = now
        markup = {
            'inline_keyboard': [
                [{'text': TEXTS[loc]['btn_human'], 'callback_data': 'support_human'}]
            ]
        }
        send(chat_id, TEXTS[loc]['limit_reached'], markup)
        return True

    session['count_today'] = count_today + 1
    session['last_at'] = now

    user_query = str(text or '')[:2000]

    session.setdefault('turns', []).append({'role': 'user', 'content': user_query[:1500]})
    session['turns'] = session['turns'][-8:]

    faq_text = _load_faq()
    selected_faq = _select_faq_context(faq_text, user_query, max_chars=12000)
    system_prompt = _build_system_prompt(selected_faq, loc)

    messages = [{'role': 'system', 'content': system_prompt}]
    for t in session['turns']:
        messages.append({'role': t.get('role', 'user'), 'content': t.get('content', '')[:1500]})

    effective_settings = _resolve_settings(settings, state)

    try:
        answer = _llm_answer(messages, effective_settings)
        if not answer or not str(answer).strip():
            raise RuntimeError('empty_llm_response')
        ans_text = str(answer).strip()
        session['turns'].append({'role': 'assistant', 'content': ans_text[:1500]})
        session['turns'] = session['turns'][-8:]
        markup = {
            'inline_keyboard': [
                [
                    {'text': TEXTS[loc]['btn_human'], 'callback_data': 'support_human'},
                    {'text': TEXTS[loc]['btn_close'], 'callback_data': 'support_close'},
                ]
            ]
        }
        send(chat_id, html.escape(ans_text), markup)  # manager.send uses parse_mode=HTML
    except Exception:
        markup = {
            'inline_keyboard': [
                [{'text': TEXTS[loc]['btn_human'], 'callback_data': 'support_human'}]
            ]
        }
        send(chat_id, TEXTS[loc]['llm_error'], markup)

    return True


def escalate(chat_id: int, user_id: int | str, state: dict, send, user_label: str = '') -> None:
    """Escalate request to human admin (Pavel)."""
    uid = str(user_id)
    support_dict = state.setdefault('support', {})
    now = int(time.time())
    today = time.strftime('%Y-%m-%d', time.gmtime(now))

    session = support_dict.setdefault(uid, {
        'active': True,
        'started_at': now,
        'last_at': now,
        'turns': [],
        'count_today': 0,
        'day': today,
        'chat_id': int(chat_id),
        'locale': 'ru',
    })

    escalated_at = session.get('escalated_at')
    loc = session.get('locale', 'ru')
    loc = loc if loc in TEXTS else 'ru'

    if escalated_at and (now - escalated_at) < 3600:
        send(chat_id, TEXTS[loc]['already_escalated'])
        return

    session['escalated_at'] = now
    if user_label:
        session['user_label'] = user_label

    label = user_label or session.get('user_label')
    if not label:
        # Check managed bots in state to construct user_label if available
        for m in state.get('managed', {}).values():
            if isinstance(m, dict) and str(m.get('owner_id')) == uid:
                uname = m.get('username') or ''
                name = m.get('name') or ''
                if uname and name:
                    label = f"@{uname} — {name}"
                elif uname:
                    label = f"@{uname}"
                elif name:
                    label = name
                break
    if not label:
        label = f"Пользователь (ID: {user_id})"

    turns = session.get('turns', [])
    if turns:
        formatted = []
        for t in turns[-8:]:
            role = "Пользователь" if t.get('role') == 'user' else "Ассистент"
            formatted.append(f"• {role}: {html.escape(str(t.get('content', '')))[:400]}")
        turns_text = "\n".join(formatted)
    else:
        turns_text = "(нет сообщений)"

    admin_id = state.get('admin_id') or PAVEL_ID
    admin_msg = (
        f"🛟 Запрос в поддержку от {label} (id: {user_id})\n"
        f"Последние сообщения:\n{turns_text}\n\n"
        f"Подсказка: /reply {user_id} текст"
    )
    send(admin_id, admin_msg)
    send(chat_id, TEXTS[loc]['escalated_user'])


def handle_callback(chat_id: int, user_id: int | str, data: str, state: dict, send, user_label: str = '') -> bool:
    """Handle support callback query ('support_human' or 'support_close')."""
    if data == 'support_human':
        escalate(chat_id, user_id, state, send, user_label=user_label)
        return True
    elif data == 'support_close':
        uid = str(user_id)
        support_dict = state.get('support', {})
        loc = 'ru'
        if uid in support_dict:
            support_dict[uid]['active'] = False
            loc = support_dict[uid].get('locale', 'ru')
        loc = loc if loc in TEXTS else 'ru'
        send(chat_id, TEXTS[loc]['closed'])
        return True
    return False


def admin_reply(user_id: int | str, text: str, state: dict, send) -> bool:
    """Send admin reply to user and record it in turns."""
    uid = str(user_id)
    support_dict = state.get('support', {})
    session = support_dict.get(uid)
    now = int(time.time())

    if session and isinstance(session, dict):
        session['escalated_at'] = None
        session['last_at'] = now
        session.setdefault('turns', []).append({'role': 'assistant', 'content': f"[Поддержка] {text}"[:1500]})
        session['turns'] = session['turns'][-8:]
        chat_id = session.get('chat_id', int(user_id))
        loc = session.get('locale', 'ru')
    else:
        chat_id = int(user_id)
        loc = 'ru'

    loc = loc if loc in TEXTS else 'ru'
    prefix = TEXTS[loc].get('admin_reply_prefix', '👨‍💻 <b>Ответ поддержки:</b>')
    send(chat_id, f"{prefix}\n{html.escape(text)}")
    return True


def expire(state: dict, now: int | float | None = None) -> list[int]:
    """Expire sessions inactive for 30 minutes."""
    if now is None:
        now = int(time.time())
    expired_users: list[int] = []
    support_dict = state.get('support', {})
    if not isinstance(support_dict, dict):
        return []

    for uid_str, session in list(support_dict.items()):
        if not isinstance(session, dict):
            continue
        if not session.get('active'):
            continue
        last_at = session.get('last_at')
        if last_at is None:
            last_at = session.get('started_at', 0)
        if now - last_at >= 1800:
            session['active'] = False
            try:
                expired_users.append(int(uid_str))
            except ValueError:
                pass
    return expired_users
