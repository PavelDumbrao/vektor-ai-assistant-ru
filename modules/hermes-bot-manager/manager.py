#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRET_FILE = Path('/etc/proai-hermes-manager.env')
ROOT = Path('/opt/proai-hermes-manager')
STATE_DIR = ROOT / 'state'
STATE_FILE = STATE_DIR / 'state.json'
MANAGED_DIR = STATE_DIR / 'managed'
PROFILE_DIR = Path('/opt/vektor/profiles')
PAVEL_ID = 450206471
BOT_USERNAME = 'ProAIHermesBot'
TOKEN_RE = re.compile(r'^\d{5,}:[A-Za-z0-9_-]{20,}$')
RUNNING = True


def load_token() -> str:
    for raw in SECRET_FILE.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        value = line.split('=', 1)[1].strip().strip('"\'')
        if TOKEN_RE.fullmatch(value):
            return value
    raise RuntimeError('manager_token_missing')


def api(method: str, payload: dict | None = None, timeout: int = 65):
    token = load_token()
    url = f'https://api.telegram.org/bot{token}/{method}'
    body = json.dumps(payload or {}).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', 'replace')[:500]
        raise RuntimeError(f'bot_api_http_{exc.code}:{raw}') from None
    if not data.get('ok'):
        raise RuntimeError('bot_api_error:' + str(data.get('description', 'unknown'))[:300])
    return data.get('result')


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {'offset': 0, 'managed': {}, 'drafts': {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {'offset': 0, 'managed': {}, 'drafts': {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.state.', dir=STATE_DIR)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(state, out, ensure_ascii=False, indent=2)
    os.chmod(name, 0o600)
    os.replace(name, STATE_FILE)


def main_menu():
    return {
        'inline_keyboard': [
            [{'text': '⚡ Создать Hermes', 'callback_data': 'create'}],
            [{'text': '🤖 Мои Hermes', 'callback_data': 'my'},
             {'text': '🧠 Как это работает', 'callback_data': 'how'}],
            [{'text': '🔐 Безопасность', 'callback_data': 'security'},
             {'text': '🛟 Помощь', 'callback_data': 'help'}],
        ]
    }


def send(chat_id: int, text: str, markup: dict | None = None):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
               'disable_web_page_preview': True}
    if markup:
        payload['reply_markup'] = markup
    return api('sendMessage', payload)


def answer_callback(callback_id: str, text: str = ''):
    payload = {'callback_query_id': callback_id}
    if text:
        payload['text'] = text[:200]
    try:
        api('answerCallbackQuery', payload)
    except Exception:
        pass


def welcome_text() -> str:
    return (
        '<b>Hermes Forge | Pro AI</b>\n\n'
        'Создай собственного AI-ассистента Hermes прямо в Telegram.\n\n'
        'Telegram создаёт бота на <b>твоём аккаунте</b>, а Hermes Forge подключает '
        'его к AI-инфраструктуре и помогает управлять доступом и токенами.\n\n'
        'Нажми «Создать Hermes», чтобы начать.'
    )


def create_keyboard(user: dict):
    uid = int(user['id'])
    first = (user.get('first_name') or 'AI').strip()[:30]
    request_id = int(time.time() * 1000) & 0x7fffffff
    suggested_name = f'Hermes | {first}'[:64]
    suggested_username = f'Hermes{uid}Bot'[:32]
    return {
        'keyboard': [[{
            'text': '⚡ Создать моего Hermes',
            'request_managed_bot': {
                'request_id': request_id,
                'suggested_name': suggested_name,
                'suggested_username': suggested_username,
            },
        }]],
        'resize_keyboard': True,
        'one_time_keyboard': True,
        'input_field_placeholder': 'Нажми кнопку ниже',
    }


def show_create(chat_id: int, user: dict):
    me = api('getMe')
    if not me.get('can_manage_bots'):
        send(chat_id,
             '⚙️ <b>Hermes Forge почти готов.</b>\n\n'
             'Bot Management Mode ещё не активирован в BotFather. '
             'Попробуй немного позже.', main_menu())
        return
    send(chat_id,
         '<b>Создание Hermes</b>\n\n'
         'Нажми кнопку ниже. Telegram откроет нативное окно создания бота. '
         'Имя и @username можно изменить перед подтверждением.\n\n'
         'Бот останется <b>твоей собственностью</b>.',
         create_keyboard(user))


def show_my(chat_id: int, user_id: int, state: dict):
    rows = []
    for item in state.get('managed', {}).values():
        if int(item.get('owner_user_id', 0)) != int(user_id):
            continue
        uname = item.get('username') or ''
        status = item.get('profile_status') or 'registered'
        rows.append(f'• <b>@{uname}</b> — {status}')
    if not rows:
        text = ('<b>Мои Hermes</b>\n\nПока нет созданных агентов. '
                'Нажми «Создать Hermes» в главном меню.')
    else:
        text = '<b>Мои Hermes</b>\n\n' + '\n'.join(rows)
    send(chat_id, text, main_menu())


def find_profile_for_owner(user_id: int):
    matches = []
    for registry in PROFILE_DIR.glob('*.json'):
        try:
            meta = json.loads(registry.read_text(encoding='utf-8'))
            owner = str(meta.get('owner') or '').strip()
            if not re.fullmatch(r'[a-z0-9_-]{2,40}', owner):
                continue
            cfg = Path(f'/home/{owner}/.hermes/config.yaml')
            if not cfg.is_file():
                continue
            text = cfg.read_text(encoding='utf-8', errors='replace')
            m = re.search(r'home_channel:\s*\n(?:\s+.*\n){0,4}?\s+chat_id:\s*[\'\"]?(\d+)', text)
            if m and int(m.group(1)) == int(user_id):
                matches.append(owner)
        except Exception:
            continue
    return matches[0] if len(matches) == 1 else None


def store_managed_token(bot_id: int, token: str) -> Path:
    if not TOKEN_RE.fullmatch(token):
        raise RuntimeError('invalid_managed_token')
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    path = MANAGED_DIR / f'{int(bot_id)}.env'
    fd, name = tempfile.mkstemp(prefix='.token.', dir=MANAGED_DIR)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write('TELEGRAM_BOT_TOKEN=' + token + '\n')
    os.chmod(name, 0o600)
    os.replace(name, path)
    return path


def install_profile_token(owner: str, token: str) -> str:
    import pwd
    if not re.fullmatch(r'[a-z0-9_-]{2,40}', owner):
        return 'invalid_profile'
    account = pwd.getpwnam(owner)
    env_path = Path(account.pw_dir) / '.hermes' / '.env'
    if not env_path.is_file() or env_path.is_symlink():
        return 'profile_env_missing'
    lines = env_path.read_text(encoding='utf-8').splitlines()
    existing = ''
    for line in lines:
        if line.startswith('TELEGRAM_BOT_TOKEN='):
            existing = line.partition('=')[2].strip()
            break
    service = f'{owner}-hermes.service'
    active = subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0
    if existing and active:
        return 'manual_review_existing_token'

    updated = []
    replaced = False
    for line in lines:
        if line.startswith('TELEGRAM_BOT_TOKEN='):
            updated.append('TELEGRAM_BOT_TOKEN=' + token)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append('TELEGRAM_BOT_TOKEN=' + token)
    fd, name = tempfile.mkstemp(prefix='.env.', dir=env_path.parent)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write('\n'.join(updated).rstrip() + '\n')
        out.flush()
        os.fsync(out.fileno())
    os.chown(name, account.pw_uid, account.pw_gid)
    os.chmod(name, 0o600)
    os.replace(name, env_path)
    subprocess.run(['systemctl', 'enable', service], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['systemctl', 'restart', service], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    active = subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0
    return 'active' if active else 'service_failed'


def managed_event(update: dict, state: dict):
    event = update.get('managed_bot') or {}
    user = event.get('user') or {}
    bot = event.get('bot') or {}
    owner_id = int(user.get('id') or 0)
    bot_id = int(bot.get('id') or 0)
    if not owner_id or not bot_id:
        return

    token = api('getManagedBotToken', {'user_id': bot_id})
    token_path = store_managed_token(bot_id, token)
    try:
        api('setManagedBotAccessSettings', {
            'user_id': bot_id,
            'is_access_restricted': True,
            'added_user_ids': [PAVEL_ID],
        })
        access = 'owner_plus_tech'
    except Exception:
        access = 'default'
    owner = find_profile_for_owner(owner_id)
    if owner:
        profile_status = install_profile_token(owner, token)
    else:
        profile_status = 'awaiting_profile'
    uname = bot.get('username') or str(bot_id)
    state.setdefault('managed', {})[str(bot_id)] = {
        'owner_user_id': owner_id,
        'profile': owner,
        'profile_status': profile_status,
        'bot_id': bot_id,
        'username': uname,
        'name': bot.get('first_name') or '',
        'access': access,
        'token_path': str(token_path),
        'updated_at': int(time.time()),
    }
    save_state(state)
    if profile_status == 'active':
        text = (f'✅ <b>Hermes готов.</b>\n\n@{uname} подключён к твоему персональному '
                'Hermes-профилю и запущен. Доступ по умолчанию закрыт для посторонних.')
    elif profile_status == 'awaiting_profile':
        text = (f'✅ <b>@{uname} создан.</b>\n\nБот уже зарегистрирован в Hermes Forge. '
                'Персональный профиль будет подключён следующим шагом.')
    else:
        text = (f'✅ <b>@{uname} создан.</b>\n\nСтатус подключения профиля: '
                f'<code>{profile_status}</code>.')
    send(owner_id, text, main_menu())
    if owner_id != PAVEL_ID:
        try:
            profile_label = owner or 'не найден'
            send(PAVEL_ID,
                 '<b>🛠 Новый Managed Hermes</b>\n\n'
                 f'Бот: <b>@{uname}</b>\n'
                 f'Профиль: <code>{profile_label}</code>\n'
                 f'Статус: <b>{profile_status}</b>\n'
                 f'Доступ: <b>{access}</b>')
        except Exception:
            pass


def handle_message(msg: dict, state: dict):
    chat = msg.get('chat') or {}
    user = msg.get('from') or {}
    chat_id = int(chat.get('id') or 0)
    user_id = int(user.get('id') or 0)
    text = (msg.get('text') or '').strip()
    if not chat_id or not user_id:
        return
    if msg.get('managed_bot_created'):
        send(chat_id, '⚙️ Бот создан. Завершаю подключение к Hermes…')
        return
    command = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
    if command in {'/start', '/menu'}:
        send(chat_id, welcome_text(), main_menu())
    elif command in {'/new', '/create'}:
        show_create(chat_id, user)
    elif command in {'/my', '/bots'}:
        show_my(chat_id, user_id, state)
    elif command == '/status':
        me = api('getMe')
        managed = sum(1 for x in state.get('managed', {}).values()
                      if int(x.get('owner_user_id', 0)) == user_id)
        send(chat_id,
             '<b>Статус Hermes Forge</b>\n\n'
             f'Management Mode: <b>{"ON" if me.get("can_manage_bots") else "OFF"}</b>\n'
             f'Твоих Hermes: <b>{managed}</b>', main_menu())
    elif command == '/help':
        send(chat_id,
             '<b>Помощь</b>\n\nСоздание: /new\nМои агенты: /my\nСтатус: /status\n'
             'Главное меню: /menu', main_menu())
    elif text:
        send(chat_id, 'Выбери действие в меню 👇', main_menu())


def handle_callback(q: dict, state: dict):
    callback_id = q.get('id') or ''
    user = q.get('from') or {}
    msg = q.get('message') or {}
    chat_id = int((msg.get('chat') or {}).get('id') or user.get('id') or 0)
    user_id = int(user.get('id') or 0)
    data = q.get('data') or ''
    answer_callback(callback_id)
    if data == 'create':
        show_create(chat_id, user)
    elif data == 'my':
        show_my(chat_id, user_id, state)
    elif data == 'how':
        send(chat_id,
             '<b>Как это работает</b>\n\n'
             '1. Ты создаёшь бота в нативном окне Telegram.\n'
             '2. Бот принадлежит тебе, не Pro AI.\n'
             '3. Hermes Forge получает техническое право подключить его к AI-инфраструктуре.\n'
             '4. Токен хранится на сервере закрыто и может быть перевыпущен.\n'
             '5. Для готового профиля запуск происходит автоматически.', main_menu())
    elif data == 'security':
        send(chat_id,
             '<b>Безопасность</b>\n\n'
             '• владельцем созданного бота остаёшься ты\n'
             '• по умолчанию доступ к managed Hermes ограничивается владельцем\n'
             '• токены не показываются в интерфейсе Hermes Forge\n'
             '• токен можно перевыпустить без пересоздания бота', main_menu())
    elif data == 'help':
        send(chat_id, '<b>Помощь</b>\n\n/new — создать Hermes\n/my — мои Hermes\n/status — статус\n/menu — меню', main_menu())


def process_update(update: dict, state: dict):
    if update.get('managed_bot'):
        managed_event(update, state)
    elif update.get('callback_query'):
        handle_callback(update['callback_query'], state)
    elif update.get('message'):
        handle_message(update['message'], state)


def stop_handler(*_):
    global RUNNING
    RUNNING = False


def run():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MANAGED_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    os.chmod(MANAGED_DIR, 0o700)
    state = load_state()
    api('deleteWebhook', {'drop_pending_updates': False})
    me = api('getMe')
    if me.get('username') != BOT_USERNAME:
        raise RuntimeError('unexpected_manager_bot')
    while RUNNING:
        try:
            updates = api('getUpdates', {
                'offset': int(state.get('offset', 0)),
                'timeout': 40,
                'limit': 50,
                'allowed_updates': ['message', 'callback_query', 'managed_bot'],
            }, timeout=50) or []
            for update in updates:
                update_id = int(update.get('update_id', 0))
                try:
                    process_update(update, state)
                except Exception as exc:
                    print('update_error=' + type(exc).__name__, flush=True)
                state['offset'] = max(int(state.get('offset', 0)), update_id + 1)
                save_state(state)
        except Exception as exc:
            print('poll_error=' + type(exc).__name__, flush=True)
            time.sleep(3)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    run()
