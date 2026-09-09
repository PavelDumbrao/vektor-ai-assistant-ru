#!/usr/bin/env python3
from manager import api, BOT_USERNAME

NAME = 'Hermes Forge | Pro AI'
SHORT = 'Кузница персональных AI-агентов Hermes. Создай своего бота в Telegram за несколько нажатий.'
DESCRIPTION = '''Hermes Forge | Pro AI — портал создания персональных AI-агентов Hermes.

⚡ Создавай собственного Telegram-бота нативно
🤖 Подключай его к персональному Hermes-профилю
🔐 Бот остаётся твоей собственностью
🧠 Память, голос, файлы, поиск и AI-инструменты
🛠 Управление доступом и безопасностью

Нажми «Начать», затем «Создать Hermes».'''
COMMANDS = [
    {'command': 'start', 'description': 'Открыть Hermes Forge'},
    {'command': 'new', 'description': 'Создать нового Hermes'},
    {'command': 'my', 'description': 'Мои Hermes-агенты'},
    {'command': 'status', 'description': 'Статус Manager и агентов'},
    {'command': 'menu', 'description': 'Главное меню'},
    {'command': 'help', 'description': 'Помощь'},
]


def main():
    me = api('getMe')
    if me.get('username') != BOT_USERNAME:
        raise RuntimeError('wrong_bot')
    checks = {}
    checks['setMyName'] = api('setMyName', {'name': NAME})
    checks['setMyShortDescription'] = api('setMyShortDescription', {'short_description': SHORT})
    checks['setMyDescription'] = api('setMyDescription', {'description': DESCRIPTION})
    checks['setMyCommands'] = api('setMyCommands', {'commands': COMMANDS})
    checks['setChatMenuButton'] = api('setChatMenuButton', {
        'menu_button': {'type': 'commands'}
    })
    me2 = api('getMe')
    print('username=' + str(me2.get('username') or ''))
    print('bot_id=' + str(me2.get('id') or ''))
    print('can_manage_bots=' + str(bool(me2.get('can_manage_bots'))).lower())
    print('profile_configured=' + str(all(v is True for v in checks.values())).lower())


if __name__ == '__main__':
    main()
