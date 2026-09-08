# Focus Assistant для Hermes/Vektor

Профильный plugin превращает Hermes в личный операционный контур:

- отдельная SQLite-БД `focus/ledger.db` для задач, обещаний, ожиданий и идей;
- реестр принципиально не связан с Kanban-dispatcher и не запускает агентов;
- подтверждённые цели в `focus/goals.yaml`;
- suppression неизменившихся напоминаний через `focus_attention`;
- read-only Fathom через личное Maton-подключение;
- read-only Google Drive и защищённые подтверждением Gmail/Calendar writes;
- тихий Fathom watcher: пустой stdout при отсутствии новых встреч.

## Установка

Запускать venv-питоном целевого профиля и от имени владельца профиля:

```bash
HERMES_HOME=/home/assistant/.hermes \
  /home/assistant/.hermes/hermes-agent/venv/bin/python \
  modules/focus-assistant/install.py \
  --hermes-home /home/assistant/.hermes
```

После установки перезапустить gateway в idle. Не сбрасывать действующую сессию. Значения секретов
plugin не копирует и не выводит; Fathom использует уже настроенный
`MCP_MATON_API_KEY`.

Для первоначальной установки прежнего Focus применяется готовая конфигурация proactive-
контуров (утро, вечер, Fathom watcher, SOUL/AGENTS):

```bash
HERMES_HOME=/home/pavel/.hermes \
  /home/pavel/.hermes/hermes-agent/venv/bin/python \
  modules/focus-assistant/configure_profile.py \
  --hermes-home /home/pavel/.hermes
```

## Семь функций и Telegram-подача, версия 1.2

В существующем профиле использовать **profile_seven.py**, а не повторный bootstrap.
Он устанавливает восемь новых native tools и skill `assistant-workflows`, закрепляет
четыре Maton connection, обновляет только Focus-блоки SOUL/AGENTS и два существующих
обзора. Не меняет config.yaml, модель, контекст, Fathom watcher или его состояние.

Сначала запуск без `--apply`: проверка expected SHA-256 config/plugin, владельца,
уникальных ACTIVE connections и совпадения Google identity. Затем idle-проверка,
остановка только этого профиля, тот же запуск с `--apply` и запуск профиля обратно.
Аргументы: `--hermes-home`, `--owner`, `--expected-config`, `--expected-plugin`.
Нужны HERMES_HOME и PYTHONPATH на установленный Hermes core. Запуск от владельца
профиля. `.env` читается на сервере, значения не выводятся.

Backup создаётся в `<home>/backups/assistant-seven-*`: предыдущий plugin, skill,
затронутые документы, jobs.json, config и консистентная копия ledger.db. При ошибке
применения файлы откатываются. Runtime core не меняется. Внешний smoke не является
частью установщика и не отправляет почту автоматически.

Новые сценарии: обязательства с цитатой, бриф, подготовка/разбор встречи, intake
голосового, почта draft/approval/send/read-back, версионируемые проекты, недельный
обзор. Письма локальные до отдельного send; изменение проекта требует approval.
Отправка после uncertain запрещена до сверки RFC Message-ID, даже после перезапуска.
Версия 1.2 дополнительно добавляет человеческую Telegram-подачу через профильный
pre_llm context и `references/08-telegram-style.md`: обычный Markdown на входе,
компактные абзацы/списки, минимум эмодзи и никаких внутренних JSON/tool names.
Hermes adapter сам преобразует это в MarkdownV2; core/config менять не нужно.

Тесты: `PYTHONPATH=hermes-agent .venv/bin/python -m pytest modules/focus-assistant/tests -q`.

## Безопасность

- Gmail/Drive/Fathom-контент считается недоверенным.
- Google Drive в этом этапе только читается.
- Gmail draft/send и Calendar write проходят одноразовый approval gate.
- Fathom client не поддерживает `destination_url`, webhooks или write-методы.
- Элементы из переписок/писем/встреч создаются как `candidate`; активация
  требует подтверждения владельца.
