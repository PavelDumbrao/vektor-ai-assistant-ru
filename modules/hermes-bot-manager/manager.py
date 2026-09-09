#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from hermes_instance import HermesInstance, normalize_bot_username, owner_for_telegram_id

SECRET_FILE = Path('/etc/proai-hermes-manager.env')
ROOT = Path('/opt/proai-hermes-manager')
STATE_DIR = ROOT / 'state'
STATE_FILE = STATE_DIR / 'state.json'
MANAGED_DIR = STATE_DIR / 'managed'
INSTANCES_DIR = STATE_DIR / 'instances'
PROVISIONING_DIR = STATE_DIR / 'provisioning'
JOBS_DIR = STATE_DIR / 'jobs'
PROVISIONER_UNIT = 'proai-hermes-provisioner@{instance_id}.service'
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


def load_manager_settings() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in SECRET_FILE.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def stable_release_id() -> str:
    value = load_manager_settings().get('FORGE_STABLE_RELEASE_ID', '').strip()
    if not value:
        raise RuntimeError('forge_stable_release_missing')
    return value


def build_instance(owner_id: int, bot: dict) -> HermesInstance:
    username = normalize_bot_username(str(bot.get('username') or ''))
    return HermesInstance(
        instance_id=f'hermes-{int(owner_id)}',
        owner_linux=owner_for_telegram_id(owner_id),
        owner_telegram_id=int(owner_id),
        bot_id=int(bot.get('id') or 0),
        bot_username=username,
        bot_name=str(bot.get('first_name') or username)[:64],
        release_id=stable_release_id(),
    ).validate()


def start_provisioning(instance: HermesInstance) -> str:
    for directory in (INSTANCES_DIR, PROVISIONING_DIR, JOBS_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    instance_file = INSTANCES_DIR / f'{instance.instance_id}.json'
    if instance_file.exists():
        current = HermesInstance.from_dict(json.loads(instance_file.read_text(encoding='utf-8')))
        if current != instance:
            raise RuntimeError('instance_desired_state_conflict')
    else:
        instance.write_atomic(instance_file)
    unit = PROVISIONER_UNIT.format(instance_id=instance.instance_id)
    result = subprocess.run(
        ['/usr/bin/systemctl', 'start', '--no-block', unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError('provisioner_unit_start_failed')
    return unit


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
        return {'offset': 0, 'managed': {}, 'imported': {}, 'drafts': {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        state.setdefault('managed', {})
        state.setdefault('imported', {})
        state.setdefault('drafts', {})
        return state
    except Exception:
        return {'offset': 0, 'managed': {}, 'imported': {}, 'drafts': {}}


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
            [{'text': '⚡ Нанять AI-ассистента', 'callback_data': 'create'}],
            [{'text': '🤖 Мои AI-ассистенты', 'callback_data': 'my'},
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


def reconcile_provisioning(state: dict) -> None:
    changed = False
    for item in state.get('managed', {}).values():
        instance_id = str(item.get('instance_id') or '')
        if not instance_id:
            continue
        receipt_path = PROVISIONING_DIR / f'{instance_id}.json'
        if receipt_path.is_symlink() or not receipt_path.is_file():
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        result_state = str(receipt.get('state') or '')
        if result_state == 'active' and item.get('profile_status') != 'active':
            item['profile_status'] = 'active'
            item['health'] = receipt.get('health') or {}
            item['updated_at'] = int(time.time())
            changed = True
        elif result_state == 'failed' and item.get('profile_status') != 'provision_failed':
            item['profile_status'] = 'provision_failed'
            item['error_code'] = str(receipt.get('error_code') or 'provision_failed')[:120]
            item['updated_at'] = int(time.time())
            changed = True

        status = item.get('profile_status')
        if status not in {'active', 'provision_failed'} or item.get('notified_state') == status:
            continue
        owner_id = int(item.get('owner_user_id') or 0)
        username = item.get('username') or ''
        try:
            if status == 'active':
                send(owner_id,
                     f'✅ <b>AI-ассистент готов к работе.</b>\n\n@{username} полностью развёрнут и прошёл проверку. Можно открывать его и начинать работать.',
                     main_menu())
            else:
                send(owner_id,
                     f'⚠️ <b>@{username} создан, но настройка Hermes не завершилась.</b>\n\nТехническая ошибка уже зафиксирована. Сам бот остаётся твоим; повторное развёртывание не требует создавать его заново.',
                     main_menu())
            item['notified_state'] = status
            changed = True
        except Exception:
            pass
        if owner_id != PAVEL_ID and item.get('admin_notified_state') != status:
            try:
                detail = item.get('error_code') or 'healthy'
                send(PAVEL_ID,
                     '<b>Hermes Forge provisioning</b>\n\n'
                     f'Бот: <b>@{username}</b>\n'
                     f'Профиль: <code>{item.get("profile") or ""}</code>\n'
                     f'Статус: <b>{status}</b>\n'
                     f'Деталь: <code>{html.escape(str(detail))}</code>')
                item['admin_notified_state'] = status
                changed = True
            except Exception:
                pass
    if changed:
        save_state(state)


def welcome_text() -> str:
    return (
        '<b>Hermes Forge | Pro AI</b>\n\n'
        'Найми своего персонального AI-ассистента прямо в Telegram.\n\n'
        '<b>Самообучающийся:</b> чем больше ты с ним работаешь и поправляешь его, тем точнее он подстраивается под тебя.\n\n'
        '<b>Постоянно развивается:</b> вместе с экосистемой Hermes он получает новые общие инструменты и навыки. Твои приватные данные при этом не смешиваются с чужими.\n\n'
        'Нажми «Нанять AI-ассистента», и я проведу тебя по шагам прямо здесь.'
    )


def hire_key(user_id: int) -> str:
    return str(int(user_id))


def username_problem(raw: str) -> tuple[str, str | None]:
    username = (raw or '').strip().lstrip('@').lower()
    problems = []
    if not username.lower().endswith('bot'):
        problems.append('username должен обязательно заканчиваться на <code>bot</code>')
    if not 5 <= len(username) <= 32:
        problems.append('длина username должна быть от 5 до 32 символов')
    if not re.fullmatch(r'[A-Za-z0-9_]+', username or ''):
        problems.append('можно использовать только латинские буквы, цифры и знак <code>_</code>')
    return username, ('; '.join(problems) if problems else None)


def final_hire_keyboard(name: str, username: str):
    encoded_name = urllib.parse.quote(name, safe='')
    url = f'https://t.me/newbot/{BOT_USERNAME}/{username}?name={encoded_name}'
    return {
        'inline_keyboard': [
            [{'text': '✅ Подтвердить найм', 'url': url}],
            [{'text': '✏️ Изменить имя', 'callback_data': 'hire_name'},
             {'text': '✏️ Изменить username', 'callback_data': 'hire_username'}],
            [{'text': '❌ Отмена', 'callback_data': 'hire_cancel'}],
        ]
    }


def ask_hire_name(chat_id: int, user_id: int, state: dict) -> None:
    state.setdefault('drafts', {})[hire_key(user_id)] = {
        'step': 'name', 'updated_at': int(time.time())
    }
    save_state(state)
    send(chat_id,
         '<b>Шаг 1 из 2. Как будет называться твой AI-ассистент?</b>\n\n'
         'Название может быть любым. Например:\n'
         '<code>Салават AI</code>\n'
         '<code>Маркус</code>\n'
         '<code>Мой ассистент</code>\n\n'
         'Просто напиши название сюда 👇',
         {'remove_keyboard': True})


def ask_hire_username(chat_id: int, user_id: int, state: dict) -> None:
    draft = state.setdefault('drafts', {}).setdefault(hire_key(user_id), {})
    draft['step'] = 'username'
    draft['updated_at'] = int(time.time())
    save_state(state)
    send(chat_id,
         '<b>Шаг 2 из 2. Теперь придумай username.</b>\n\n'
         'Это адрес твоего бота в Telegram.\n\n'
         'ВАЖНО: username <b>обязательно должен заканчиваться на bot</b>.\n'
         'Только строчные латинские буквы <code>a-z</code>, цифры и <code>_</code>. Длина 5–32 символа.\n'
        'Если напишешь заглавные буквы, я сам приведу их к строчным.\n\n'
         'Примеры:\n'
         '<code>salavatai_bot</code>\n'
         '<code>markushelperbot</code>\n\n'
         'Напиши username сюда. Можно с @ или без него 👇')


def show_hire_confirm(chat_id: int, user_id: int, state: dict) -> None:
    draft = state.get('drafts', {}).get(hire_key(user_id)) or {}
    name = str(draft.get('name') or '').strip()
    username = str(draft.get('username') or '').strip()
    if not name or not username:
        ask_hire_name(chat_id, user_id, state)
        return
    draft['step'] = 'confirm'
    draft['updated_at'] = int(time.time())
    save_state(state)
    send(chat_id,
         '<b>Всё готово. Проверь:</b>\n\n'
         f'Имя: <b>{html.escape(name)}</b>\n'
         f'Username: <b>@{username}</b>\n\n'
         'Что произойдёт дальше:\n'
         '1. Нажмёшь «Подтвердить найм».\n'
         '2. Telegram покажет одно системное подтверждение создания бота. Ничего заново вводить не нужно.\n'
         '3. Бот создаётся <b>в твоём Telegram-аккаунте</b>, как при создании через BotFather, и принадлежит тебе.\n'
         '4. Hermes Forge автоматически подключит его к твоему AI-ассистенту.\n\n'
         'Если Telegram скажет, что username уже занят, просто вернись сюда и отправь новый username обычным сообщением.',
         final_hire_keyboard(name, username))


def show_create(chat_id: int, user: dict, state: dict):
    me = api('getMe')
    if not me.get('can_manage_bots'):
        send(chat_id,
             '⚙️ <b>Сервис найма AI-ассистента временно недоступен.</b>\n\n'
             'Попробуй немного позже.', main_menu())
        return
    ask_hire_name(chat_id, int(user['id']), state)


def _profile_owner_telegram_id(owner: str) -> int:
    if not re.fullmatch(r'[a-z0-9_-]{2,40}', owner):
        raise RuntimeError('import_profile_invalid_owner')
    registry = PROFILE_DIR / f'{owner}.json'
    if registry.is_symlink() or not registry.is_file():
        raise RuntimeError('import_profile_registry_missing')
    meta = json.loads(registry.read_text(encoding='utf-8'))
    if str(meta.get('owner') or '') != owner:
        raise RuntimeError('import_profile_registry_mismatch')
    cfg = Path(f'/home/{owner}/.hermes/config.yaml')
    if cfg.is_symlink() or not cfg.is_file():
        raise RuntimeError('import_profile_config_missing')
    text = cfg.read_text(encoding='utf-8', errors='replace')
    match = re.search(r'home_channel:\s*\n(?:\s+.*\n){0,6}?\s+chat_id:\s*[\'\"]?(\d+)', text)
    if not match:
        raise RuntimeError('import_profile_owner_chat_missing')
    return int(match.group(1))


def _profile_bot_identity(owner: str) -> dict:
    env_path = Path(f'/home/{owner}/.hermes/.env')
    if env_path.is_symlink() or not env_path.is_file():
        raise RuntimeError('import_profile_env_missing')
    token = ''
    for raw in env_path.read_text(encoding='utf-8').splitlines():
        if raw.startswith('TELEGRAM_BOT_TOKEN='):
            token = raw.partition('=')[2].strip().strip('"\'')
            break
    if not TOKEN_RE.fullmatch(token):
        raise RuntimeError('import_profile_token_missing')
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/getMe')
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        raise RuntimeError('import_profile_getme_failed') from exc
    bot = data.get('result') or {}
    bot_id = int(bot.get('id') or 0)
    username = str(bot.get('username') or '').strip().lower()
    if not bot_id or not username:
        raise RuntimeError('import_profile_identity_invalid')
    return {
        'bot_id': bot_id,
        'username': username,
        'name': str(bot.get('first_name') or '').strip(),
    }


def import_existing_profile(owner: str, state: dict) -> dict:
    owner = str(owner or '').strip().lower()
    owner_user_id = _profile_owner_telegram_id(owner)
    bot = _profile_bot_identity(owner)
    bot_id = int(bot['bot_id'])
    item = {
        'owner_user_id': owner_user_id,
        'profile': owner,
        'profile_status': 'active' if _imported_service_active({'profile': owner}) else 'inactive',
        'bot_id': bot_id,
        'username': bot['username'],
        'name': bot['name'],
        'source': 'existing_profile',
        'imported_at': int(time.time()),
    }
    state.setdefault('imported', {})[str(bot_id)] = item
    save_state(state)
    return item

def _imported_service_active(item: dict) -> bool:
    owner = str(item.get('profile') or '')
    if not re.fullmatch(r'[a-z0-9_-]{2,40}', owner):
        return False
    service = f'{owner}-hermes.service'
    return subprocess.run(
        ['/usr/bin/systemctl', 'is-active', '--quiet', service],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def show_my(chat_id: int, user_id: int, state: dict):
    rows = []
    seen_bot_ids = set()
    for bot_id, item in state.get('managed', {}).items():
        if int(item.get('owner_user_id', 0)) != int(user_id):
            continue
        uname = item.get('username') or bot_id
        status = item.get('profile_status') or 'registered'
        rows.append(f'• <b>@{uname}</b> — {status}')
        seen_bot_ids.add(str(item.get('bot_id') or bot_id))
    for bot_id, item in state.get('imported', {}).items():
        if int(item.get('owner_user_id', 0)) != int(user_id):
            continue
        if str(item.get('bot_id') or bot_id) in seen_bot_ids:
            continue
        uname = item.get('username') or bot_id
        status = 'работает' if _imported_service_active(item) else 'остановлен'
        rows.append(f'• <b>@{uname}</b> — {status} · подключён ранее')
    if not rows:
        text = ('<b>Мои AI-ассистенты</b>\n\nПока никого не наняли. '
                'Нажми «Нанять AI-ассистента» в главном меню.')
    else:
        text = '<b>Мои AI-ассистенты</b>\n\n' + '\n'.join(rows)
    send(chat_id, text, main_menu())


def is_admin(user_id: int) -> bool:
    return int(user_id) == PAVEL_ID


def is_authorized_user(user_id: int, state: dict) -> bool:
    uid = int(user_id)
    if is_admin(uid):
        return True
    if str(uid) in state.get('allowed_users', {}):
        return True
    return find_profile_for_owner(uid) is not None


def access_denied(chat_id: int) -> None:
    try:
        send(chat_id, '🔒 <b>Доступ закрыт.</b>\n\nHermes Forge работает только по приглашению администратора Pro AI.')
    except Exception:
        pass


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
    if not is_authorized_user(owner_id, state):
        try:
            send(PAVEL_ID,
                 '<b>🚫 Заблокирована попытка Managed Hermes</b>\n\n'
                 f'Telegram ID: <code>{owner_id}</code>\n'
                 'Пользователь не был авторизован, токен бота не запрашивался.')
        except Exception:
            pass
        access_denied(owner_id)
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
    instance = None
    provision_job = None
    provision_error = ''
    if owner:
        profile_status = install_profile_token(owner, token)
    else:
        try:
            instance = build_instance(owner_id, bot)
            owner = instance.owner_linux
            provision_job = start_provisioning(instance)
            profile_status = 'provisioning'
        except Exception as exc:
            owner = owner_for_telegram_id(owner_id)
            profile_status = 'provision_failed'
            provision_error = str(exc)[:120] or type(exc).__name__
    uname = (bot.get('username') or str(bot_id)).lower()
    state.setdefault('managed', {})[str(bot_id)] = {
        'owner_user_id': owner_id,
        'profile': owner,
        'profile_status': profile_status,
        'bot_id': bot_id,
        'username': uname,
        'name': bot.get('first_name') or '',
        'access': access,
        'token_path': str(token_path),
        'instance_id': instance.instance_id if instance else '',
        'release_id': instance.release_id if instance else '',
        'provision_job': provision_job,
        'error_code': provision_error,
        'updated_at': int(time.time()),
    }
    state.setdefault('drafts', {}).pop(hire_key(owner_id), None)
    save_state(state)
    if profile_status == 'active':
        text = (f'✅ <b>AI-ассистент нанят и готов к работе.</b>\n\n@{uname} уже подключён и запущен. '
                'Он будет запоминать твой рабочий контекст и учиться на твоих правках. Доступ для посторонних закрыт.')
    elif profile_status == 'provisioning':
        text = (f'✅ <b>@{uname} создан.</b>\n\nТеперь Hermes Forge автоматически разворачивает персональную среду: память, runtime, базу и инструменты. '
                'Когда health-check пройдёт, я напишу сюда, что ассистент готов.')
    else:
        text = (f'⚠️ <b>@{uname} создан, но Hermes пока не развёрнут.</b>\n\n'
                'Ошибка зафиксирована безопасно. Бота заново создавать не нужно.')
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


def handle_hire_text(chat_id: int, user_id: int, text: str, state: dict) -> bool:
    drafts = state.setdefault('drafts', {})
    draft = drafts.get(hire_key(user_id))
    if not isinstance(draft, dict):
        return False
    step = draft.get('step')
    if step == 'name':
        name = ' '.join((text or '').split()).strip()
        if not name:
            send(chat_id, 'Название не может быть пустым. Напиши любое имя для ассистента 👇')
            return True
        if len(name) > 64:
            send(chat_id,
                 f'Название слишком длинное: {len(name)} символов. Максимум 64. '
                 'Сократи название и отправь ещё раз 👇')
            return True
        draft['name'] = name
        draft['updated_at'] = int(time.time())
        save_state(state)
        ask_hire_username(chat_id, user_id, state)
        return True
    if step == 'username':
        username, problem = username_problem(text)
        if problem:
            details = problem.replace('; ', '\n• ')
            send(chat_id,
                 '<b>Нужно немного поправить username:</b>\n\n'
                 '• ' + details + '\n\n'
                 'Пример правильного варианта: <code>SalavatAI_bot</code>\n\n'
                 'Отправь исправленный username сюда 👇')
            return True
        draft['username'] = username
        draft['updated_at'] = int(time.time())
        save_state(state)
        show_hire_confirm(chat_id, user_id, state)
        return True
    if step == 'confirm':
        username, problem = username_problem(text)
        if problem:
            details = problem.replace('; ', '\n• ')
            send(chat_id,
                 'Если хочешь поменять username, пришли новый вариант. Нужно исправить:\n\n'
                 '• ' + details + '\n\nПример: <code>SalavatAI_bot</code>')
            return True
        draft['username'] = username
        draft['updated_at'] = int(time.time())
        save_state(state)
        show_hire_confirm(chat_id, user_id, state)
        return True
    return False

def handle_message(msg: dict, state: dict):
    chat = msg.get('chat') or {}
    user = msg.get('from') or {}
    chat_id = int(chat.get('id') or 0)
    user_id = int(user.get('id') or 0)
    text = (msg.get('text') or '').strip()
    if not chat_id or not user_id:
        return
    # Forge is private-chat only. Group/channel updates are ignored.
    if chat.get('type') != 'private':
        return
    if not is_authorized_user(user_id, state):
        access_denied(chat_id)
        return
    if msg.get('managed_bot_created'):
        send(chat_id, '⚙️ Бот создан. Завершаю подключение к Hermes…')
        return
    parts = text.split()
    command = parts[0].split('@')[0].lower() if text.startswith('/') and parts else ''

    # Admin-only migration of already-running legacy Hermes profiles.
    if command == '/importprofile':
        if not is_admin(user_id):
            access_denied(chat_id)
            return
        if len(parts) != 2:
            send(chat_id, 'Формат: <code>/importprofile PROFILE</code>', main_menu())
            return
        try:
            item = import_existing_profile(parts[1], state)
        except Exception as exc:
            send(chat_id,
                 '<b>Импорт не выполнен.</b>\n\n'
                 f'Код: <code>{html.escape(str(exc))[:120]}</code>', main_menu())
            return
        send(chat_id,
             '<b>✅ Существующий AI-ассистент добавлен в Forge.</b>\n\n'
             f'@{item["username"]}\nПрофиль: <code>{item["profile"]}</code>\n'
             'Бот не пересоздавался, текущий токен остался на месте.', main_menu())
        return

    # Admin-only admission control for future clients.
    if command in {'/allow', '/deny', '/allowed'}:
        if not is_admin(user_id):
            access_denied(chat_id)
            return
        if command == '/allowed':
            explicit = sorted(int(x) for x in state.get('allowed_users', {}) if str(x).isdigit())
            body = '\n'.join(f'• <code>{uid}</code>' for uid in explicit) or '• нет дополнительных ID'
            send(chat_id, '<b>Явно разрешённые пользователи</b>\n\n' + body +
                 '\n\nВладельцы заранее созданных Hermes-профилей разрешаются автоматически.', main_menu())
            return
        if len(parts) != 2 or not parts[1].isdigit():
            send(chat_id, f'Формат: <code>{command} TELEGRAM_ID</code>', main_menu())
            return
        target = int(parts[1])
        if target <= 0 or target == PAVEL_ID:
            send(chat_id, 'Некорректный Telegram ID.', main_menu())
            return
        allowed = state.setdefault('allowed_users', {})
        if command == '/allow':
            allowed[str(target)] = {'added_by': PAVEL_ID, 'added_at': int(time.time())}
            result = f'✅ <code>{target}</code> разрешён.'
        else:
            allowed.pop(str(target), None)
            result = f'🔒 <code>{target}</code> удалён из явного allowlist.'
        save_state(state)
        send(chat_id, result, main_menu())
        return

    if command == '/cancel':
        state.setdefault('drafts', {}).pop(hire_key(user_id), None)
        save_state(state)
        send(chat_id, 'Ок, найм отменён. Когда будешь готов, нажми «Нанять AI-ассистента».', main_menu())
    elif command in {'/start', '/menu'}:
        state.setdefault('drafts', {}).pop(hire_key(user_id), None)
        save_state(state)
        send(chat_id, welcome_text(), main_menu())
    elif command in {'/new', '/create', '/hire'}:
        show_create(chat_id, user, state)
    elif not command and text and handle_hire_text(chat_id, user_id, text, state):
        return
    elif command in {'/my', '/bots'}:
        show_my(chat_id, user_id, state)
    elif command == '/status':
        managed_ids = {
            str(x.get('bot_id') or bot_id) for bot_id, x in state.get('managed', {}).items()
            if int(x.get('owner_user_id', 0)) == user_id
        }
        imported_ids = {
            str(x.get('bot_id') or bot_id) for bot_id, x in state.get('imported', {}).items()
            if int(x.get('owner_user_id', 0)) == user_id
        }
        total = len(managed_ids | imported_ids)
        send(chat_id,
             '<b>Твои AI-ассистенты</b>\n\n'
             f'Подключено: <b>{total}</b>', main_menu())
    elif command == '/help':
        send(chat_id,
             '<b>Помощь</b>\n\n/hire — нанять AI-ассистента\n/my — мои ассистенты\n'
             '/cancel — отменить текущий найм\n/menu — главное меню', main_menu())
    elif text:
        send(chat_id, 'Нажми «⚡ Нанять AI-ассистента», и я проведу тебя по шагам.', main_menu())


def handle_callback(q: dict, state: dict):
    callback_id = q.get('id') or ''
    user = q.get('from') or {}
    msg = q.get('message') or {}
    chat_id = int((msg.get('chat') or {}).get('id') or user.get('id') or 0)
    user_id = int(user.get('id') or 0)
    data = q.get('data') or ''
    chat_type = (msg.get('chat') or {}).get('type')
    if not chat_id or not user_id or chat_type != 'private':
        answer_callback(callback_id, 'Доступ закрыт')
        return
    if not is_authorized_user(user_id, state):
        answer_callback(callback_id, 'Доступ закрыт')
        access_denied(chat_id)
        return
    answer_callback(callback_id)
    if data == 'create':
        show_create(chat_id, user, state)
    elif data == 'hire_name':
        ask_hire_name(chat_id, user_id, state)
    elif data == 'hire_username':
        draft = state.get('drafts', {}).get(hire_key(user_id)) or {}
        if draft.get('name'):
            ask_hire_username(chat_id, user_id, state)
        else:
            ask_hire_name(chat_id, user_id, state)
    elif data == 'hire_cancel':
        state.setdefault('drafts', {}).pop(hire_key(user_id), None)
        save_state(state)
        send(chat_id, 'Найм отменён.', main_menu())
    elif data == 'my':
        show_my(chat_id, user_id, state)
    elif data == 'how':
        send(chat_id,
             '<b>Как это работает</b>\n\n'
             '1. Ты даёшь ассистенту имя и username прямо в этом чате.\n'
             '2. Telegram просит одно финальное подтверждение.\n'
             '3. Бот создаётся в твоём аккаунте, как через BotFather, и принадлежит тебе.\n'
             '4. Мы автоматически подключаем к нему Hermes.\n'
             '5. Ассистент запоминает твой контекст и учится на твоих правках: чем больше работаешь с ним, тем точнее он подстраивается под тебя.\n'
             '6. По мере развития экосистемы Hermes он получает новые общие инструменты и навыки. Твои личные данные при этом не смешиваются с данными других людей.', main_menu())
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
    for directory in (STATE_DIR, MANAGED_DIR, INSTANCES_DIR, PROVISIONING_DIR, JOBS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    state = load_state()
    api('deleteWebhook', {'drop_pending_updates': False})
    me = api('getMe')
    if me.get('username') != BOT_USERNAME:
        raise RuntimeError('unexpected_manager_bot')
    while RUNNING:
        try:
            reconcile_provisioning(state)
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
            reconcile_provisioning(state)
        except Exception as exc:
            print('poll_error=' + type(exc).__name__, flush=True)
            time.sleep(3)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    run()
