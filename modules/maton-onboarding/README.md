# Maton onboarding для Вектора

Модуль позволяет владельцу подключить личный Maton API key внутри приватного
Telegram-чата с Вектором, не передавая ключ в LLM, сессии Hermes или логи.

## Контракт безопасности

1. Владелец отправляет `/maton` в личном чате со своим ботом.
2. Следующее сообщение перехватывается hook `pre_gateway_dispatch` до auth,
   логирования, сессии и LLM.
3. Бот сначала удаляет сообщение через Telegram Bot API. Если удалить не
   удалось, ключ не сохраняется.
4. Ключ проверяется read-only запросом к `https://api.maton.ai/connections`.
5. Валидный ключ атомарно записывается как `MCP_MATON_API_KEY` в `.env` mode
   `0600`; `mcp_servers.maton.enabled` переключается в `true`.
6. Hermes перезагружает MCP. Значение ключа никогда не возвращается и не
   логируется.

До появления валидного ключа Maton MCP остаётся выключенным, поэтому новый
ассистент не получает 401/cooldown и продолжает нормально отвечать в Telegram.

## Установка

Запускать venv-питоном установленного Hermes:

```bash
~/.hermes/hermes-agent/venv/bin/python \
  <repo>/modules/maton-onboarding/install.py \
  --hermes-home ~/.hermes
```

После установки перезапустить gateway. От владельца нужен только `/maton` и
личный API key из Maton → Settings.

## Подключение сервисов

После проверки ключа пользователь пишет обычным языком: «Подключи Google
Drive». Ассистент использует `search_apps` → `create_connection`, присылает URL
авторизации и проверяет `get_connection`. Подключение считается готовым только
при `status=ACTIVE`.

