#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import shutil
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
import i18n

SECRET_FILE = Path('/etc/proai-hermes-manager.env')
ROOT = Path('/opt/proai-hermes-manager')
STATE_DIR = ROOT / 'state'
STATE_FILE = STATE_DIR / 'state.json'
MANAGED_DIR = STATE_DIR / 'managed'
INSTANCES_DIR = STATE_DIR / 'instances'
PROVISIONING_DIR = STATE_DIR / 'provisioning'
JOBS_DIR = STATE_DIR / 'jobs'
TOKEN_UPDATES_DIR = STATE_DIR / 'token-updates'
OFFBOARDED_DIR = STATE_DIR / 'offboarded'
PROVISIONER_UNIT = 'proai-hermes-provisioner@{instance_id}.service'
PROFILE_DIR = Path('/opt/vektor/profiles')
PAVEL_ID = 450206471
BOT_USERNAME = 'ProAIHermesBot'
DEFAULT_FORGE_WEBAPP_URL = 'https://forge.srv1250550.hstgr.cloud/'
TOKEN_RE = re.compile(r'^\d{5,}:[A-Za-z0-9_-]{20,}$')
PROVISION_DEADLINE_S = 1200
INSTANCE_OWNERS: set[int] | None = None
RUNNING = True


def log_hire_event(bot_id: int | str, user_id: int | str, stage: str, result: str) -> None:
    print(f"hire.event bot_id={bot_id} user_id={user_id} stage={stage} result={result}", flush=True)


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


def forge_webapp_url() -> str:
    try:
        configured = load_manager_settings().get('FORGE_WEBAPP_URL', '').strip()
    except OSError:
        configured = ''
    candidate = configured or DEFAULT_FORGE_WEBAPP_URL
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return DEFAULT_FORGE_WEBAPP_URL
    if (
        parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
    ):
        return DEFAULT_FORGE_WEBAPP_URL
    path = parsed.path or '/'
    return urllib.parse.urlunsplit(('https', parsed.netloc, path, '', ''))


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


def start_provisioning(instance: HermesInstance, overwrite: bool = False) -> str:
    for directory in (INSTANCES_DIR, PROVISIONING_DIR, JOBS_DIR, TOKEN_UPDATES_DIR, OFFBOARDED_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    instance_file = INSTANCES_DIR / f'{instance.instance_id}.json'
    if instance_file.exists():
        if overwrite:
            instance.write_atomic(instance_file)
        else:
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
        return {
            'offset': 0, 'managed': {}, 'imported': {}, 'drafts': {},
            'locales': {}, 'allowed_users': {}, 'denied_users': {},
            'pending_confirmations': {},
        }
    try:
        content = STATE_FILE.read_text(encoding='utf-8')
        state = json.loads(content)
        if not isinstance(state, dict):
            raise ValueError('state_not_a_dict')
        state.setdefault('managed', {})
        state.setdefault('imported', {})
        state.setdefault('drafts', {})
        state.setdefault('locales', {})
        state.setdefault('allowed_users', {})
        state.setdefault('denied_users', {})
        state.setdefault('pending_confirmations', {})
        return state
    except Exception as exc:
        ts = int(time.time())
        corrupt_path = STATE_DIR / f'state.json.corrupt-{ts}'
        try:
            os.replace(STATE_FILE, corrupt_path)
        except Exception:
            pass
        bak_path = STATE_DIR / 'state.json.bak'
        if bak_path.is_file():
            try:
                state = json.loads(bak_path.read_text(encoding='utf-8'))
                if isinstance(state, dict):
                    state.setdefault('managed', {})
                    state.setdefault('imported', {})
                    state.setdefault('drafts', {})
                    state.setdefault('locales', {})
                    state.setdefault('allowed_users', {})
                    state.setdefault('denied_users', {})
                    state.setdefault('pending_confirmations', {})
                    return state
            except Exception:
                pass
        print(f"ERROR: state.json is corrupt and no valid .bak found: {exc}", file=sys.stderr)
        if os.environ.get('FORGE_MANAGER_ALLOW_EMPTY_STATE') == '1':
            return {
                'offset': 0, 'managed': {}, 'imported': {}, 'drafts': {},
                'locales': {}, 'allowed_users': {}, 'denied_users': {},
                'pending_confirmations': {},
            }
        sys.exit(2)


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    bak_path = STATE_DIR / 'state.json.bak'
    if STATE_FILE.exists():
        try:
            shutil.copy2(STATE_FILE, bak_path)
        except Exception:
            pass
    fd, name = tempfile.mkstemp(prefix='.state.', dir=STATE_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(state, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, STATE_FILE)
        try:
            dir_fd = os.open(str(STATE_DIR), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
    except Exception:
        if os.path.exists(name):
            try:
                os.unlink(name)
            except Exception:
                pass
        raise


def rebuild_instance_owners() -> set[int]:
    global INSTANCE_OWNERS
    owners: set[int] = set()
    if INSTANCES_DIR.is_dir():
        for p in INSTANCES_DIR.glob('*.json'):
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
                oid = data.get('owner_telegram_id')
                if oid is not None:
                    owners.add(int(oid))
            except Exception:
                continue
    INSTANCE_OWNERS = owners
    return owners


def is_admin(user_id: int) -> bool:
    return int(user_id) == PAVEL_ID


def is_authorized_user(user_id: int, state: dict) -> bool:
    global INSTANCE_OWNERS
    uid = int(user_id)
    # Check denied_users first
    denied = state.get('denied_users', {})
    if str(uid) in denied or uid in denied:
        return False
    if is_admin(uid):
        return True
    allowed = state.get('allowed_users', {})
    if str(uid) in allowed or uid in allowed:
        return True
    for item in state.get('managed', {}).values():
        if isinstance(item, dict):
            oid = item.get('owner_user_id') or item.get('owner_id')
            if oid is not None and int(oid) == uid:
                return True
    for item in state.get('imported', {}).values():
        if isinstance(item, dict):
            oid = item.get('owner_user_id') or item.get('owner_id')
            if oid is not None and int(oid) == uid:
                return True
    if INSTANCE_OWNERS is None:
        rebuild_instance_owners()
    if uid in INSTANCE_OWNERS:
        return True
    return False


def access_denied(chat_id: int, locale: str = "ru") -> None:
    try:
        send(chat_id, i18n.t(locale, 'access_denied'))
    except Exception:
        pass


def find_profile_for_owner(user_id: int):
    # WARNING: Untrusted data source. This reads config.yaml from client home directory.
    # Kept strictly for the admin /importprofile command. Do not use for authorization or routing.
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
    MANAGED_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = MANAGED_DIR / f'{int(bot_id)}.env'
    fd, name = tempfile.mkstemp(prefix='.token.', dir=MANAGED_DIR)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write('TELEGRAM_BOT_TOKEN=' + token + '\n')
        out.flush()
        os.fsync(out.fileno())
    os.chmod(name, 0o600)
    os.replace(name, path)
    return path


def install_profile_token(owner: str, token: str, allow_replace: bool = False) -> str:
    if not re.fullmatch(r'[a-z0-9_-]{2,40}', owner):
        return 'invalid_profile'
    registry_file = PROFILE_DIR / f'{owner}.json'
    instance_found = False
    if INSTANCES_DIR.is_dir():
        for p in INSTANCES_DIR.glob('*.json'):
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
                if data.get('owner_linux') == owner:
                    instance_found = True
                    break
            except Exception:
                pass
    if not registry_file.is_file() and not instance_found:
        return 'profile_missing'

    TOKEN_UPDATES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_path = TOKEN_UPDATES_DIR / f'{owner}.json'

    # Do not overwrite existing token unless explicit admin replace
    if not allow_replace:
        if job_path.is_file():
            return 'manual_review_existing_token'
        if instance_found:
            return 'manual_review_existing_token'

    job_data = {
        'owner': owner,
        'token': token,
        'created_at': int(time.time()),
        'status': 'pending_operator',
    }
    fd, name = tempfile.mkstemp(prefix='.token-update.', dir=TOKEN_UPDATES_DIR)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(job_data, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(name, 0o600)
    os.replace(name, job_path)

    # Provisioner contract does not include a token-update command.
    # Mark task pending_operator and notify admin with /retry.
    try:
        send(PAVEL_ID,
             f"⚠️ Задание обновления токена для <code>{owner}</code> требует оператора.\n"
             f"Команда: <code>/retry {owner}</code>")
    except Exception:
        pass
    return 'pending_operator'


def complete_token_and_provision(bot_id: int, item: dict, state: dict, locale: str | None = None) -> bool:
    owner_id = int(item.get('owner_user_id') or item.get('owner_id') or 0)
    uname = item.get('username') or str(bot_id)
    bot_name = item.get('name') or uname
    if locale is None:
        locale = i18n.locale_for(owner_id, state)

    try:
        log_hire_event(bot_id, owner_id, 'token_fetch', 'start')
        token = api('getManagedBotToken', {'user_id': bot_id}, timeout=15)
        token_path = store_managed_token(bot_id, token)
        log_hire_event(bot_id, owner_id, 'token_stored', str(token_path))
        item['token_path'] = str(token_path)

        try:
            api('setManagedBotAccessSettings', {
                'user_id': bot_id,
                'is_access_restricted': True,
                'added_user_ids': [PAVEL_ID],
            })
            access = 'owner_plus_tech'
        except Exception:
            access = 'default'
        item['access'] = access

        instance = build_instance(owner_id, {'id': bot_id, 'username': uname, 'first_name': bot_name})
        item['attempt'] = int(item.get('attempt') or 0) + 1
        item['provision_started_at'] = int(time.time())
        item['instance_id'] = instance.instance_id
        item['release_id'] = instance.release_id
        item['profile'] = instance.owner_linux

        provision_job = start_provisioning(instance)
        item['provision_job'] = provision_job
        item['profile_status'] = 'provisioning'
        item['updated_at'] = int(time.time())
        item.pop('error_code', None)
        log_hire_event(bot_id, owner_id, 'provisioning_started', provision_job)
        save_state(state)
        rebuild_instance_owners()

        send(owner_id,
             i18n.t(locale, 'managed_provisioning', username=html.escape(uname)),
             main_menu(locale))
        if owner_id != PAVEL_ID:
            try:
                send(PAVEL_ID,
                     '<b>🛠 Новый Managed Hermes</b>\n\n'
                     f'Бот: <b>@{uname}</b>\n'
                     f'Профиль: <code>{instance.owner_linux}</code>\n'
                     f'Статус: <b>provisioning</b>\n'
                     f'Доступ: <b>{access}</b>')
            except Exception:
                pass
        return True
    except Exception as exc:
        retry_count = int(item.get('retry_count') or 0) + 1
        item['retry_count'] = retry_count
        item['last_error'] = str(exc)[:120] or type(exc).__name__
        item['updated_at'] = int(time.time())
        log_hire_event(bot_id, owner_id, 'token_fetch_error', f"{type(exc).__name__}:{str(exc)[:60]}")

        if retry_count >= 5:
            item['profile_status'] = 'provision_failed'
            item['error_code'] = 'token_fetch_failed'
            log_hire_event(bot_id, owner_id, 'token_fetch_failed', 'max_retries_exceeded')
            save_state(state)
            send(owner_id,
                 i18n.t(locale, 'managed_failed', username=html.escape(uname)),
                 main_menu(locale))
            if owner_id != PAVEL_ID:
                try:
                    send(PAVEL_ID, f"⚠️ Не удалось получить токен для @{uname} (bot_id={bot_id}) после 5 попыток: {exc}")
                except Exception:
                    pass
        else:
            delay = min(600, 30 * (2 ** (retry_count - 1)))
            item['next_retry_at'] = int(time.time()) + delay
            save_state(state)
            if retry_count == 1:
                send(owner_id,
                     i18n.t(locale, 'token_fetch_delay', username=html.escape(uname)),
                     main_menu(locale))
        return False


def managed_event(update: dict, state: dict):
    event = update.get('managed_bot') or {}
    user = event.get('user') or {}
    bot = event.get('bot') or {}
    owner_id = int(user.get('id') or 0)
    bot_id = int(bot.get('id') or 0)
    if not owner_id or not bot_id:
        return

    if not is_authorized_user(owner_id, state):
        log_hire_event(bot_id, owner_id, 'unauthorized', 'access_denied')
        locale = i18n.locale_for(owner_id, state, user)
        try:
            send(PAVEL_ID,
                 '<b>🚫 Заблокирована попытка Managed Hermes</b>\n\n'
                 f'Telegram ID: <code>{owner_id}</code>\n'
                 'Пользователь не был авторизован, токен бота не запрашивался.')
        except Exception:
            pass
        access_denied(owner_id, locale)
        return

    locale = i18n.remember_locale(user, state)
    bot_key = str(bot_id)

    if bot_key in state.get('managed', {}):
        log_hire_event(bot_id, owner_id, 'managed_bot_duplicate', 'ignored')
        existing_item = state['managed'][bot_key]
        if bot.get('username'):
            existing_item['username'] = normalize_bot_username(str(bot['username']))
        if bot.get('first_name'):
            existing_item['name'] = str(bot['first_name'])[:64]
        save_state(state)
        return

    uname = normalize_bot_username(str(bot.get('username') or bot_id))
    bot_name = str(bot.get('first_name') or uname)[:64]
    now = int(time.time())

    item = {
        'owner_user_id': owner_id,
        'owner_id': owner_id,
        'bot_id': bot_id,
        'username': uname,
        'name': bot_name,
        'profile': owner_for_telegram_id(owner_id),
        'profile_status': 'token_pending',
        'created_at': now,
        'updated_at': now,
        'retry_count': 0,
        'next_retry_at': 0,
        'attempt': 0,
    }
    state.setdefault('managed', {})[bot_key] = item
    state.setdefault('drafts', {}).pop(hire_key(owner_id), None)
    save_state(state)
    log_hire_event(bot_id, owner_id, 'token_pending', 'saved')

    complete_token_and_provision(bot_id, item, state, locale)


def main_menu(locale: str = "ru"):
    return {
        'inline_keyboard': [
            [{'text': i18n.t(locale, 'menu_hire'), 'callback_data': 'create'}],
            [{'text': i18n.t(locale, 'menu_panel'), 'web_app': {'url': forge_webapp_url()}}],
            [{'text': i18n.t(locale, 'menu_my'), 'callback_data': 'my'},
             {'text': i18n.t(locale, 'menu_how'), 'callback_data': 'how'}],
            [{'text': i18n.t(locale, 'menu_security'), 'callback_data': 'security'},
             {'text': i18n.t(locale, 'menu_help'), 'callback_data': 'help'}],
            [{'text': i18n.t(locale, 'menu_language'), 'callback_data': 'language'}],
        ]
    }


def send(chat_id: int, text: str, markup: dict | None = None):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
               'disable_web_page_preview': True}
    if markup:
        payload['reply_markup'] = markup
    return api('sendMessage', payload, timeout=10)


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
    now = int(time.time())

    for bot_id_str, item in list(state.get('managed', {}).items()):
        if not isinstance(item, dict):
            continue
        if item.get('profile_status') == 'token_pending':
            next_retry = int(item.get('next_retry_at') or 0)
            if now >= next_retry:
                bot_id = int(item.get('bot_id') or bot_id_str)
                complete_token_and_provision(bot_id, item, state)

    for bot_id_str, item in list(state.get('managed', {}).items()):
        if not isinstance(item, dict):
            continue
        instance_id = str(item.get('instance_id') or '')
        if not instance_id:
            continue
        receipt_path = PROVISIONING_DIR / f'{instance_id}.json'
        receipt = None
        if receipt_path.is_file() and not receipt_path.is_symlink():
            try:
                raw_receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                if isinstance(raw_receipt, dict):
                    item_bot_id = int(item.get('bot_id') or bot_id_str)
                    owner_id = int(item.get('owner_user_id') or item.get('owner_id') or 0)
                    r_bot_id = raw_receipt.get('bot_id')
                    if r_bot_id is not None:
                        if int(r_bot_id) == item_bot_id:
                            receipt = raw_receipt
                    else:
                        owner_entries = [
                            b for b, m in state.get('managed', {}).items()
                            if isinstance(m, dict) and int(m.get('owner_user_id') or m.get('owner_id') or 0) == owner_id
                        ]
                        if len(owner_entries) == 1:
                            receipt = raw_receipt

                    if receipt is not None:
                        p_started = int(item.get('provision_started_at') or 0)
                        r_updated = receipt.get('updated_at')
                        if r_updated is None:
                            r_updated = receipt.get('started_at')
                        if p_started and r_updated is not None:
                            if int(r_updated) < p_started:
                                receipt = None
            except Exception:
                receipt = None

        status = item.get('profile_status')

        if receipt is not None:
            result_state = str(receipt.get('state') or '')
            if result_state == 'active' and status != 'active':
                item['profile_status'] = 'active'
                item['health'] = receipt.get('health') or {}
                item['updated_at'] = int(time.time())
                log_hire_event(item.get('bot_id', bot_id_str), item.get('owner_user_id', 0), 'provisioning_active', 'success')
                changed = True
            elif result_state == 'failed':
                if status == 'active':
                    item['last_error'] = str(receipt.get('error_code') or 'health_failed')[:120]
                    item['updated_at'] = int(time.time())
                    if item.get('last_health_alert') != item['last_error']:
                        try:
                            send(PAVEL_ID,
                                 f"⚠️ <b>Health Alert: @{item.get('username')}</b>\n\n"
                                 f"Профиль: <code>{item.get('profile')}</code>\n"
                                 f"Ошибка: <code>{html.escape(item['last_error'])}</code>")
                            item['last_health_alert'] = item['last_error']
                        except Exception:
                            pass
                    changed = True
                elif status != 'provision_failed':
                    item['profile_status'] = 'provision_failed'
                    item['error_code'] = str(receipt.get('error_code') or 'provision_failed')[:120]
                    item['updated_at'] = int(time.time())
                    log_hire_event(item.get('bot_id', bot_id_str), item.get('owner_user_id', 0), 'provisioning_failed', item['error_code'])
                    changed = True

        elif status == 'provisioning':
            p_started = int(item.get('provision_started_at') or 0)
            is_timeout = (p_started > 0 and (now - p_started) > PROVISION_DEADLINE_S)
            unit_failed_or_inactive = False
            if not is_timeout:
                unit = f'proai-hermes-provisioner@{instance_id}.service'
                try:
                    res = subprocess.run(
                        ['/usr/bin/systemctl', 'show', '-p', 'ActiveState,Result', '--value', unit],
                        capture_output=True, text=True, timeout=10, check=False,
                    )
                    lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
                    active_state = lines[0] if lines else ''
                    if active_state in ('failed', 'inactive'):
                        unit_failed_or_inactive = True
                except Exception:
                    pass

            if is_timeout or unit_failed_or_inactive:
                item['profile_status'] = 'provision_failed'
                item['error_code'] = 'provision_timeout' if is_timeout else 'provisioner_exited'
                item['updated_at'] = now
                log_hire_event(item.get('bot_id', bot_id_str), item.get('owner_user_id', 0), 'provisioning_failed', item['error_code'])
                changed = True

        cur_status = item.get('profile_status')
        if cur_status in {'active', 'provision_failed'} and item.get('notified_state') != cur_status:
            owner_id = int(item.get('owner_user_id') or item.get('owner_id') or 0)
            username = item.get('username') or ''
            locale = i18n.locale_for(owner_id, state)
            err_code = item.get('error_code') or ''
            if cur_status == 'active':
                key = 'provision_active'
            elif err_code in ('provision_timeout', 'provisioner_exited'):
                key = 'provision_delayed'
            else:
                key = 'provision_failed'
            try:
                send(owner_id,
                     i18n.t(locale, key, username=html.escape(str(username))),
                     main_menu(locale))
                item['notified_state'] = cur_status
                changed = True
            except Exception:
                pass
            if owner_id != PAVEL_ID and item.get('admin_notified_state') != cur_status:
                try:
                    detail = item.get('error_code') or 'healthy'
                    send(PAVEL_ID,
                         '<b>Hermes Forge provisioning</b>\n\n'
                         f'Бот: <b>@{username}</b>\n'
                         f'Профиль: <code>{item.get("profile") or ""}</code>\n'
                         f'Статус: <b>{cur_status}</b>\n'
                         f'Деталь: <code>{html.escape(str(detail))}</code>')
                    item['admin_notified_state'] = cur_status
                    changed = True
                except Exception:
                    pass

    if changed:
        save_state(state)


def welcome_text(locale: str = "ru") -> str:
    return i18n.t(locale, 'welcome')


def hire_key(user_id: int) -> str:
    return str(int(user_id))


def username_problem(raw: str, locale: str = "ru") -> tuple[str, str | None]:
    username = (raw or '').strip().lstrip('@').lower()
    problems = []
    if not username.endswith('bot'):
        problems.append(i18n.t(locale, 'username_suffix'))
    if not 5 <= len(username) <= 32:
        problems.append(i18n.t(locale, 'username_length'))
    if not re.fullmatch(r'[A-Za-z0-9_]+', username or ''):
        problems.append(i18n.t(locale, 'username_chars'))
    return username, ('; '.join(problems) if problems else None)


def final_hire_keyboard(name: str, username: str, locale: str = "ru"):
    encoded_name = urllib.parse.quote(name, safe='')
    url = f'https://t.me/newbot/{BOT_USERNAME}/{username}?name={encoded_name}'
    return {'inline_keyboard': [
        [{'text': i18n.t(locale, 'confirm_hire'), 'url': url}],
        [{'text': i18n.t(locale, 'edit_name'), 'callback_data': 'hire_name'},
         {'text': i18n.t(locale, 'edit_username'), 'callback_data': 'hire_username'}],
        [{'text': i18n.t(locale, 'cancel'), 'callback_data': 'hire_cancel'}],
    ]}


def ask_hire_name(chat_id: int, user_id: int, state: dict) -> None:
    locale = i18n.locale_for(user_id, state)
    state.setdefault('drafts', {})[hire_key(user_id)] = {
        'step': 'name', 'updated_at': int(time.time())
    }
    save_state(state)
    send(chat_id, i18n.t(locale, 'hire_name'), {'remove_keyboard': True})


def ask_hire_username(chat_id: int, user_id: int, state: dict) -> None:
    locale = i18n.locale_for(user_id, state)
    draft = state.setdefault('drafts', {}).setdefault(hire_key(user_id), {})
    draft['step'] = 'username'
    draft['updated_at'] = int(time.time())
    save_state(state)
    send(chat_id, i18n.t(locale, 'hire_username'))


def show_hire_confirm(chat_id: int, user_id: int, state: dict) -> None:
    locale = i18n.locale_for(user_id, state)
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
         i18n.t(locale, 'hire_confirm', name=html.escape(name), username=html.escape(username)),
         final_hire_keyboard(name, username, locale))


def show_create(chat_id: int, user: dict, state: dict):
    user_id = int(user['id'])
    locale = i18n.locale_for(user_id, state, user)

    user_managed = [
        m for m in state.get('managed', {}).values()
        if isinstance(m, dict) and int(m.get('owner_user_id') or m.get('owner_id') or 0) == user_id
    ]
    user_imported = [
        m for m in state.get('imported', {}).values()
        if isinstance(m, dict) and int(m.get('owner_user_id') or m.get('owner_id') or 0) == user_id
    ]

    in_progress = [b for b in user_managed if b.get('profile_status') in ('provisioning', 'token_pending')]
    if in_progress:
        send(chat_id, i18n.t(locale, 'hire_already_provisioning'), main_menu(locale))
        return

    active_bots = [b for b in user_managed if b.get('profile_status') == 'active'] + user_imported
    if active_bots:
        uname = active_bots[0].get('username') or str(active_bots[0].get('bot_id', ''))
        send(chat_id, i18n.t(locale, 'hire_already_active', username=uname), main_menu(locale))
        if user_id != PAVEL_ID:
            try:
                send(PAVEL_ID, f"ℹ️ Пользователь <code>{user_id}</code> запросил второго бота, имея активного @{uname}.")
            except Exception:
                pass
        return

    me = api('getMe')
    if not me.get('can_manage_bots'):
        send(chat_id, i18n.t(locale, 'service_unavailable'), main_menu(locale))
        return
    ask_hire_name(chat_id, user_id, state)


def _profile_owner_telegram_id(owner: str) -> int:
    # WARNING: Untrusted client data. Used only for admin /importprofile.
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
    # WARNING: Untrusted client data. Used only for admin /importprofile.
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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
    ).returncode == 0


def show_my(chat_id: int, user_id: int, state: dict):
    locale = i18n.locale_for(user_id, state)
    rows = []
    seen_bot_ids = set()
    for bot_id, item in state.get('managed', {}).items():
        if int(item.get('owner_user_id', 0)) != int(user_id):
            continue
        uname = item.get('username') or bot_id
        status = item.get('profile_status') or 'registered'
        label = i18n.t(locale, 'row_active') if status == 'active' else html.escape(str(status))
        rows.append(f'• <b>@{html.escape(str(uname))}</b> — {label}')
        seen_bot_ids.add(str(item.get('bot_id') or bot_id))
    for bot_id, item in state.get('imported', {}).items():
        if int(item.get('owner_user_id', 0)) != int(user_id):
            continue
        if str(item.get('bot_id') or bot_id) in seen_bot_ids:
            continue
        uname = item.get('username') or bot_id
        status = i18n.t(locale, 'row_active') if _imported_service_active(item) else i18n.t(locale, 'row_inactive')
        rows.append(f'• <b>@{html.escape(str(uname))}</b> — {status} · {i18n.t(locale, "row_imported")}')
    text = (i18n.t(locale, 'my_empty') if not rows else
            i18n.t(locale, 'my_title') + '\n\n' + '\n'.join(rows))
    send(chat_id, text, main_menu(locale))


def handle_hire_text(chat_id: int, user_id: int, text: str, state: dict) -> bool:
    locale = i18n.locale_for(user_id, state)
    drafts = state.setdefault('drafts', {})
    draft = drafts.get(hire_key(user_id))
    if not isinstance(draft, dict):
        return False
    step = draft.get('step')
    if step == 'name':
        name = ' '.join((text or '').split()).strip()
        if not name:
            send(chat_id, i18n.t(locale, 'name_empty'))
            return True
        if len(name) > 64:
            send(chat_id, i18n.t(locale, 'name_too_long', length=len(name)))
            return True
        draft['name'] = name
        draft['updated_at'] = int(time.time())
        save_state(state)
        ask_hire_username(chat_id, user_id, state)
        return True
    if step == 'username':
        username, problem = username_problem(text, locale)
        if problem:
            details = problem.replace('; ', '\n• ')
            send(chat_id, i18n.t(locale, 'username_fix', details=details))
            return True
        draft['username'] = username
        draft['updated_at'] = int(time.time())
        save_state(state)
        show_hire_confirm(chat_id, user_id, state)
        return True
    if step == 'confirm':
        username, problem = username_problem(text, locale)
        if problem:
            details = problem.replace('; ', '\n• ')
            send(chat_id, i18n.t(locale, 'username_change', details=details))
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
    if not chat_id or not user_id or chat.get('type') != 'private':
        return

    # Do not save locale for unauthorized users
    if not is_authorized_user(user_id, state):
        locale = i18n.locale_for(user_id, state, user)
        access_denied(chat_id, locale)
        return

    locale_key = str(user_id)
    previous_locale = state.setdefault('locales', {}).get(locale_key)
    locale = i18n.remember_locale(user, state)
    if previous_locale != locale:
        save_state(state)

    if msg.get('managed_bot_created'):
        send(chat_id, i18n.t(locale, 'created_finishing'))
        return

    parts = text.split()
    command = parts[0].split('@')[0].lower() if text.startswith('/') and parts else ''

    if command == '/importprofile':
        if not is_admin(user_id):
            access_denied(chat_id, locale)
            return
        if len(parts) != 2:
            send(chat_id, 'Формат: <code>/importprofile PROFILE</code>', main_menu(locale))
            return
        try:
            item = import_existing_profile(parts[1], state)
        except Exception as exc:
            send(chat_id, '<b>Импорт не выполнен.</b>\n\n' +
                 f'Код: <code>{html.escape(str(exc))[:120]}</code>', main_menu(locale))
            return
        send(chat_id,
             '<b>✅ Существующий AI-ассистент добавлен в Forge.</b>\n\n'
             f'@{item["username"]}\nПрофиль: <code>{item["profile"]}</code>\n'
             'Бот не пересоздавался, текущий токен остался на месте.', main_menu(locale))
        return

    if command in {'/allow', '/deny', '/allowed'}:
        if not is_admin(user_id):
            access_denied(chat_id, locale)
            return
        if command == '/allowed':
            explicit = sorted(int(x) for x in state.get('allowed_users', {}) if str(x).isdigit())
            body_text = '\n'.join(f'• <code>{uid}</code>' for uid in explicit) or '• нет дополнительных ID'
            send(chat_id, '<b>Явно разрешённые пользователи</b>\n\n' + body_text +
                 '\n\nВладельцы заранее созданных Hermes-профилей разрешаются автоматически.', main_menu(locale))
            return
        if len(parts) != 2 or not parts[1].isdigit():
            send(chat_id, f'Формат: <code>{command} TELEGRAM_ID</code>', main_menu(locale))
            return
        target = int(parts[1])
        if target <= 0 or target == PAVEL_ID:
            send(chat_id, 'Некорректный Telegram ID.', main_menu(locale))
            return
        allowed = state.setdefault('allowed_users', {})
        denied = state.setdefault('denied_users', {})
        if command == '/allow':
            denied.pop(str(target), None)
            allowed[str(target)] = {'added_by': PAVEL_ID, 'added_at': int(time.time())}
            result = f'✅ <code>{target}</code> разрешён.'
        else:
            allowed.pop(str(target), None)
            denied[str(target)] = {'denied_by': PAVEL_ID, 'denied_at': int(time.time())}
            result = f'🔒 <code>{target}</code> заблокирован (доступ запрещён).'
        save_state(state)
        send(chat_id, result, main_menu(locale))
        return

    if command == '/tenants':
        if not is_admin(user_id):
            access_denied(chat_id, locale)
            return
        rows = []
        for bot_id, item in state.get('managed', {}).items():
            owner = item.get('profile') or f"u{item.get('owner_user_id')}"
            uname = item.get('username') or bot_id
            status = item.get('profile_status') or 'unknown'
            attempt = item.get('attempt', 0)
            upd = time.strftime('%Y-%m-%d %H:%M', time.localtime(item.get('updated_at', 0))) if item.get('updated_at') else '-'
            err = item.get('error_code') or item.get('last_error') or '-'
            rows.append(f"• <b>{owner}</b> (@{uname})\n  Статус: <code>{status}</code> | Попытка: {attempt}\n  Обновлено: {upd}\n  Ошибка: <code>{html.escape(str(err))}</code>")
        body = '\n\n'.join(rows) if rows else 'Нет зарегистрированных тенантов.'
        send(chat_id, f"<b>Таблица тенантов Forge:</b>\n\n{body}", main_menu(locale))
        return

    if command == '/failed':
        if not is_admin(user_id):
            access_denied(chat_id, locale)
            return
        rows = []
        for bot_id, item in state.get('managed', {}).items():
            status = item.get('profile_status')
            if status in ('provision_failed', 'token_pending'):
                owner = item.get('profile') or f"u{item.get('owner_user_id')}"
                uname = item.get('username') or bot_id
                reason = item.get('error_code') or item.get('last_error') or 'неизвестно'
                attempt = item.get('attempt', 0)
                rows.append(f"• <b>@{uname}</b> (owner: <code>{owner}</code>, ID: <code>{bot_id}</code>)\n  Статус: <code>{status}</code>\n  Причина: <code>{html.escape(str(reason))}</code>\n  Попытка: {attempt}")
        body = '\n\n'.join(rows) if rows else 'Нет сбойных записей.'
        send(chat_id, f"<b>Сбойные тенанты Forge:</b>\n\n{body}", main_menu(locale))
        return

    if command == '/retry':
        if not is_admin(user_id):
            access_denied(chat_id, locale)
            return
        if len(parts) != 2:
            send(chat_id, 'Формат: <code>/retry BOT_ID|TG_ID</code>', main_menu(locale))
            return
        target = parts[1].strip()
        found_bot_id = None
        target_item = None
        for b_id, it in state.get('managed', {}).items():
            if (str(b_id) == target or
                str(it.get('bot_id')) == target or
                str(it.get('owner_user_id')) == target or
                str(it.get('owner_id')) == target or
                it.get('profile') == target):
                found_bot_id = int(it.get('bot_id') or b_id)
                target_item = it
                break
        if not target_item or found_bot_id is None:
            send(chat_id, f'Запись для <code>{target}</code> не найдена.', main_menu(locale))
            return

        status = target_item.get('profile_status')
        if status == 'token_pending':
            success = complete_token_and_provision(found_bot_id, target_item, state)
            res = "успешно запущен" if success else f"ошибка: {target_item.get('last_error')}"
            send(chat_id, f"Повторный шаг получения токена для @{target_item.get('username')}: {res}.", main_menu(locale))
            return
        elif status == 'provision_failed':
            target_item['attempt'] = int(target_item.get('attempt') or 0) + 1
            target_item['provision_started_at'] = int(time.time())
            target_item['profile_status'] = 'provisioning'
            target_item.pop('error_code', None)
            owner_id = int(target_item.get('owner_user_id') or target_item.get('owner_id') or 0)
            instance = build_instance(owner_id, {
                'id': found_bot_id,
                'username': target_item.get('username'),
                'first_name': target_item.get('name'),
            })
            target_item['instance_id'] = instance.instance_id
            target_item['release_id'] = instance.release_id
            INSTANCES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            instance.write_atomic(INSTANCES_DIR / f'{instance.instance_id}.json')
            unit = start_provisioning(instance, overwrite=True)
            target_item['provision_job'] = unit
            target_item['updated_at'] = int(time.time())
            save_state(state)
            rebuild_instance_owners()
            send(chat_id, f"Повторный запуск провижинера для @{target_item.get('username')} (попытка {target_item['attempt']}).", main_menu(locale))
            return
        else:
            send(chat_id, f"Статус @{target_item.get('username')} — <code>{status}</code>. Повторный запуск не требуется.", main_menu(locale))
            return

    if command == '/offboard':
        if not is_admin(user_id):
            access_denied(chat_id, locale)
            return
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat_id, 'Формат: <code>/offboard TELEGRAM_ID [confirm]</code>', main_menu(locale))
            return
        target_id = int(parts[1])
        is_confirm = len(parts) >= 3 and parts[2].lower() == 'confirm'
        confirm_key = f"offboard:{target_id}"
        now = int(time.time())

        if not is_confirm:
            state.setdefault('pending_confirmations', {})[confirm_key] = {'expires_at': now + 300}
            save_state(state)
            send(chat_id,
                 f"⚠️ <b>Подтверждение offboard для {target_id}</b>\n\n"
                 f"Для подтверждения отправьте:\n<code>/offboard {target_id} confirm</code>\n\n"
                 "(команда активна 5 минут)", main_menu(locale))
            return

        pending = state.get('pending_confirmations', {}).get(confirm_key)
        if not pending or now > int(pending.get('expires_at') or 0):
            state.setdefault('pending_confirmations', {}).pop(confirm_key, None)
            save_state(state)
            send(chat_id, f"Срок подтверждения для {target_id} истёк или не запрашивался. Повторите <code>/offboard {target_id}</code>.", main_menu(locale))
            return

        state.setdefault('pending_confirmations', {}).pop(confirm_key, None)
        instance_id = f"hermes-{target_id}"

        target_item = None
        bot_id = None
        for section in ('managed', 'imported'):
            for b_id, it in state.get(section, {}).items():
                if isinstance(it, dict) and int(it.get('owner_user_id') or it.get('owner_id') or 0) == target_id:
                    target_item = it
                    bot_id = int(it.get('bot_id') or b_id)
                    break
            if target_item:
                break
        # Legacy (imported) clients keep their named Linux profile; Forge-hired ones are h<tg_id>.
        owner_linux = str((target_item or {}).get('profile') or '') or owner_for_telegram_id(target_id)
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,39}', owner_linux):
            send(chat_id, f'Некорректное имя профиля для <code>{target_id}</code>.', main_menu(locale))
            return

        subprocess.run(['/usr/bin/systemctl', 'disable', '--now', f'{owner_linux}-hermes.service'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        subprocess.run(['/usr/bin/systemctl', 'disable', '--now', f'{owner_linux}-living-memory.timer'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        subprocess.run(['/usr/bin/systemctl', 'disable', '--now', f'{owner_linux}-living-memory.service'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)

        for unit in (f'{owner_linux}-hermes-passive-secretary-retention.timer',
                     f'proai-hermes-shared-metrics-export@{owner_linux}.timer',
                     f'proai-hermes-shared-metrics-export@{owner_linux}.service'):
            subprocess.run(['/usr/bin/systemctl', 'disable', '--now', unit],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)

        subprocess.run(['/usr/local/sbin/lanectl', 'remove', '--client', owner_linux],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)

        dest_dir = OFFBOARDED_DIR / f'{now}-{owner_linux}'
        dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(dest_dir, 0o700)

        if bot_id:
            token_file = MANAGED_DIR / f'{bot_id}.env'
            if token_file.is_file():
                try:
                    shutil.move(str(token_file), str(dest_dir / f'{bot_id}.env'))
                except Exception:
                    pass

        inst_file = INSTANCES_DIR / f'{instance_id}.json'
        if inst_file.is_file():
            try:
                shutil.move(str(inst_file), str(dest_dir / f'{instance_id}.json'))
            except Exception:
                pass

        receipt_file = PROVISIONING_DIR / f'{instance_id}.json'
        if receipt_file.is_file():
            try:
                shutil.move(str(receipt_file), str(dest_dir / f'{instance_id}.json'))
            except Exception:
                pass

        if target_item:
            target_item['profile_status'] = 'offboarded'
            target_item['offboarded_at'] = now
            target_item['updated_at'] = now
        save_state(state)
        rebuild_instance_owners()
        log_hire_event(bot_id or target_id, target_id, 'offboard', 'completed')

        owner_locale = i18n.locale_for(target_id, state)
        uname = target_item.get('username', '') if target_item else str(target_id)
        try:
            send(target_id, i18n.t(owner_locale, 'offboard_notice', username=uname), main_menu(owner_locale))
        except Exception:
            pass

        manual_msg = (
            f"✅ <b>Клиент {owner_linux} (ID: <code>{target_id}</code>) выведен из эксплуатации.</b>\n\n"
            f"Файлы перемещены в: <code>{dest_dir}</code>\n\n"
            "<b>Ручные шаги для окончательной зачистки:</b>\n"
            f"1. Удалить Linux-пользователя:\n<code>userdel -r {owner_linux}</code>\n"
            '2. Удалить базу PostgreSQL (ночной бэкап лежит в /var/backups/proai-hermes):\n'
            f'<code>docker exec passive-secretary-postgres sh -c \'PGPASSWORD="$POSTGRES_PASSWORD" dropdb -U "$POSTGRES_USER" -h localhost hermes_{owner_linux}\'</code>\n'
            f"3. Проверить реестр профилей:\n<code>rm -f /opt/vektor/profiles/{owner_linux}.json</code>"
        )
        send(chat_id, manual_msg, main_menu(locale))
        return

    if command == '/cancel':
        state.setdefault('drafts', {}).pop(hire_key(user_id), None)
        save_state(state)
        send(chat_id, i18n.t(locale, 'hire_cancelled_menu'), main_menu(locale))
    elif command in {'/start', '/menu'}:
        state.setdefault('drafts', {}).pop(hire_key(user_id), None)
        save_state(state)
        send(chat_id, welcome_text(locale), main_menu(locale))
    elif command in {'/language', '/lang'}:
        send(chat_id, i18n.t(locale, 'language_title'), i18n.language_keyboard())
    elif command in {'/new', '/create', '/hire'}:
        show_create(chat_id, user, state)
    elif not command and text and handle_hire_text(chat_id, user_id, text, state):
        return
    elif command in {'/my', '/bots'}:
        show_my(chat_id, user_id, state)
    elif command == '/status':
        managed_ids = {str(x.get('bot_id') or bot_id) for bot_id, x in state.get('managed', {}).items()
                       if int(x.get('owner_user_id', 0)) == user_id}
        imported_ids = {str(x.get('bot_id') or bot_id) for bot_id, x in state.get('imported', {}).items()
                        if int(x.get('owner_user_id', 0)) == user_id}
        send(chat_id, i18n.t(locale, 'status_summary', count=len(managed_ids | imported_ids)), main_menu(locale))
    elif command == '/help':
        send(chat_id, i18n.t(locale, 'help'), main_menu(locale))
    elif text:
        send(chat_id, i18n.t(locale, 'hire_prompt'), main_menu(locale))


def handle_callback(q: dict, state: dict):
    callback_id = q.get('id') or ''
    user = q.get('from') or {}
    msg = q.get('message') or {}
    chat_id = int((msg.get('chat') or {}).get('id') or user.get('id') or 0)
    user_id = int(user.get('id') or 0)
    data = q.get('data') or ''
    chat_type = (msg.get('chat') or {}).get('type')
    if not chat_id or not user_id or chat_type != 'private':
        answer_callback(callback_id, 'Access denied')
        return

    # Do not save locale for unauthorized users
    if not is_authorized_user(user_id, state):
        answer_callback(callback_id, 'Access denied')
        locale = i18n.locale_for(user_id, state, user)
        access_denied(chat_id, locale)
        return

    locale_key = str(user_id)
    previous_locale = state.setdefault('locales', {}).get(locale_key)
    locale = i18n.remember_locale(user, state)
    if previous_locale != locale:
        save_state(state)

    answer_callback(callback_id)

    if data.startswith('lang:'):
        selected = data.split(':', 1)[1]
        if selected not in i18n.SUPPORTED:
            return
        i18n.set_locale(state, user_id, selected)
        save_state(state)
        send(chat_id,
             i18n.t(selected, 'language_set', language=i18n.language_name(selected)),
             main_menu(selected))
        return

    if data == 'language':
        send(chat_id, i18n.t(locale, 'language_title'), i18n.language_keyboard())
    elif data == 'create':
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
        send(chat_id, i18n.t(locale, 'hire_cancelled'), main_menu(locale))
    elif data == 'my':
        show_my(chat_id, user_id, state)
    elif data == 'how':
        send(chat_id, i18n.t(locale, 'how'), main_menu(locale))
    elif data == 'security':
        send(chat_id, i18n.t(locale, 'security'), main_menu(locale))
    elif data == 'help':
        send(chat_id, i18n.t(locale, 'help'), main_menu(locale))


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
    for directory in (STATE_DIR, MANAGED_DIR, INSTANCES_DIR, PROVISIONING_DIR, JOBS_DIR, TOKEN_UPDATES_DIR, OFFBOARDED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    state = load_state()
    rebuild_instance_owners()
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
