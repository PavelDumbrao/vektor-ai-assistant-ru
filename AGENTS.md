# Инструкция для ИИ-агента-установщика

Ты — агент (Claude Code, Codex или аналог), которому человек дал этот репозиторий со словами «установи мне ассистента». Человек — новичок: не проси его работать в терминале, всё делай сам. От него потребуются только три вещи, которые машина сделать не может:

1. создать телеграм-бота у @BotFather и дать тебе токен (инструкция для человека: `docs/01-telegram-bot.md`);
2. дать ключ LLM-провайдера ИЛИ сказать, какая у него подписка (`docs/02-llm-provider.md`);
3. написать боту первое сообщение, когда всё готово.

Язык общения с человеком — русский. Ассистент называется «Вектор».

## Что ты устанавливаешь

Hermes-agent 0.20.0 (проверенный снапшот, лежит в `hermes-agent/` этого репо — НЕ клонируй апстрим, используй именно его) + готовая русская конфигурация из `config/`. Результат: телеграм-бот, который ведёт диалог, делает документы Word/PDF, расшифровывает голосовые, ставит напоминания и помнит контекст.

## Сценарий A: компьютер человека (Mac / Linux / Windows+WSL)

Запускай шаги строго по порядку, после каждого — проверка. Если проверка не прошла — чини, не иди дальше.

### A1. Зависимости

```bash
# uv (менеджер Python-окружений)
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh

# инструменты документов
# macOS:
brew install pandoc poppler zip || true
brew install --cask libreoffice || true
# Debian/Ubuntu/WSL:
# apt-get install -y pandoc poppler-utils zip libreoffice-writer libreoffice-calc libreoffice-impress
```

`node`/`npm` не обязательны для базовой работы.

### A2. Размещение файлов

```bash
mkdir -p ~/.hermes
cp -R <репо>/hermes-agent ~/.hermes/hermes-agent
cp <репо>/config/config.yaml.example ~/.hermes/config.yaml
cp <репо>/config/SOUL.md.example    ~/.hermes/SOUL.md
cp <репо>/config/.env.example       ~/.hermes/.env
chmod 600 ~/.hermes/.env
mkdir -p ~/hermes-workspace
```

В `config.yaml` замени: `terminal.cwd: /home/assistant/workspace` → реальный путь `~/hermes-workspace` (разверни `~`). `YOUR_TELEGRAM_ID` → Telegram ID человека (спроси его; узнать ID он может у бота @userinfobot).

### A3. Окружение Python

```bash
cd ~/.hermes/hermes-agent
export UV_NO_CONFIG=1
uv venv --python 3.11 venv
uv pip install --python venv/bin/python -r <репо>/config/frozen-requirements.txt
uv pip install --python venv/bin/python -e . --no-deps
uv pip install --python venv/bin/python python-docx openpyxl python-pptx pypdf reportlab
chmod -R go-w venv
```

Проверка: `venv/bin/python -c "import hermes_cli; print('ok')"` → `ok`.
Грабля: если `hermes_cli` не найден — ты пропустил `-e . --no-deps`.

### A4. Секреты

Заполни `~/.hermes/.env` (значения даёт человек):
- `TELEGRAM_BOT_TOKEN` — от @BotFather;
- `TELEGRAM_ALLOWED_USERS` — Telegram ID человека;
- `LLM_API_KEY` — по `docs/02-llm-provider.md`. Блок `model:` в `config.yaml` приведи в соответствие выбранному варианту (провайдер/модель/base_url) по той же инструкции. Для OAuth-входов запускай визард полным путём: `~/.hermes/hermes-agent/venv/bin/hermes model` (голый `hermes` не в PATH).

Никогда не выводи значения ключей в чат и не коммить их.

### A5. Хук чистых документов

```bash
sudo install -d /usr/local/bin && sudo install -m 755 <репо>/scripts/clean-docmeta.py /usr/local/bin/hermes-clean-docmeta
# или без sudo: install -m 755 ... ~/.local/bin/hermes-clean-docmeta и поправь путь в config.yaml
```

В `config.yaml` уже есть блок `hooks:` с этим скриптом и `hooks_auto_accept: true`. Задай окружение хука: скрипт читает `HERMES_DOC_WORKSPACE` (= рабочий каталог из A2) и `HERMES_DOC_AUTHOR` (= имя человека) — пропиши их в `.env`. ВАЖНО: не добавляй `matcher` в hooks — он матчит имя тула (`execute_code`), а не toolset, и с неверным значением хук молча не сработает.

### A6. Запуск и проверка

```bash
cd ~/.hermes/hermes-agent
HERMES_HOME=~/.hermes venv/bin/python -m hermes_cli.main gateway run
```

Первый запуск держи на переднем плане и читай лог. Успех = баннер «Hermes Gateway Starting» и подключение к Telegram без ошибок. Затем попроси человека написать боту «Привет». Ответил по-русски — работает.

Смоук-набор (попроси человека отправить, по одному):
1. «Сделай служебную записку о покупке принтера, пришли файлом Word» → должен прийти **файл вложением** (не текст «файл готов»!). Если файла нет — в ответе бота не было `MEDIA:<путь>`; проверь SOUL.md на месте и начни новую сессию командой `/new`.
2. «Напомни через 3 минуты: проверка» → через 3 минуты придёт напоминание.
3. Голосовое сообщение → бот поймёт текст.

Для постоянной работы оформи автозапуск: macOS — launchd-агент, Linux — systemd из `config/hermes.service.template` (замени `@USER@`). Скажи человеку честно: пока компьютер выключен/спит — бот молчит; для 24/7 см. `docs/03-server.md`.

## Сценарий B: сервер (24/7)

Человек арендует VPS по `docs/03-server.md` (Hostinger или Timeweb, **Ubuntu 24.04**, 2 ГБ RAM минимум, 4 ГБ комфортно) и даёт тебе SSH-доступ. Дальше всё как в сценарии A, плюс:

- репозиторий клонируй в общедоступное место: `/opt/vektor-ai-assistant-ru` (root:root, 755) — НЕ в /root, иначе пользователь assistant не прочитает файлы; используй этот путь как `<репо>`;

- создай отдельного пользователя: `useradd -m -s /bin/bash assistant`; всё ставь под ним (`su assistant -c ...`), home = `/home/assistant`;
- `apt-get install -y pandoc poppler-utils zip libreoffice-writer libreoffice-calc libreoffice-impress` и отдельно `apt-get install -y python3-psycopg python3-psycopg-pool` (пакет psycopg3 есть только в Ubuntu 24.04+ — ещё одна причина не брать 22.04);
- systemd: возьми `config/hermes.service.template`, замени `@USER@` → `assistant`, положи в `/etc/systemd/system/assistant-hermes.service`, `systemctl daemon-reload && systemctl enable --now assistant-hermes.service`;
- после `uv`-установки обязательно `chmod -R go-w` на venv И на `~/.local/share/uv/python` (иначе модуль секретаря позже откажется ставиться: `owner_runtime_unsafe`).

**Переезд с компьютера на сервер** (если ассистент уже работал локально): сначала останови локальный процесс gateway — два процесса на одном токене бота работать не могут. Затем перенеси на сервер содержимое локального `~/.hermes` БЕЗ `hermes-agent/venv` (память, kanban, сессии, SOUL.md, config.yaml, .env), после чего выполни установку venv по A3 уже на сервере и поправь пути в config.yaml на серверные.

## Пассивный секретарь (архив переписки) — продвинутый модуль

Что даёт: ассистент видит переписку человека в Telegram (через «Telegram для бизнеса»), расшифровывает голосовые, отвечает на вопросы «что было с Ивановым», «подведи итоги недели». Требования: **Telegram Premium у человека**, PostgreSQL, сервер (на ноутбуке смысла мало). Ставь только если человек попросил и Premium есть.

Порядок (сервер, от root; подробности в `modules/passive-secretary/README.md`):

0. **НЕ запускай `install_core_patch.py`** — ядро в `hermes-agent/` этого репо уже новее замороженного патча; README модуля используй как справку, шаги установки — только отсюда.
1. PostgreSQL: провижионер сам создаёт cluster-env, поднимает контейнер и прописывает секреты в `.env` профиля (нужен установленный Docker: `apt-get install -y docker.io docker-compose-v2`):
   `/usr/bin/python3 -I modules/passive-secretary-postgres/provision_postgres.py provision --client-id assistant --tenant-id assistant --owner assistant --hermes-env /home/assistant/.hermes/.env`
2. Скопируй `modules/passive-secretary` в `/opt/hermes-passive-secretary` (владелец root, файлы не writable группе/остальным). Установщику нужен root-owned `uv` в системном PATH: `install -m 755 /home/assistant/.local/bin/uv /usr/local/bin/uv`.
3. `cd /opt/hermes-passive-secretary && python3 install_passive_secretary.py install --client assistant --owner assistant --hermes-home /home/assistant/.hermes --tenant-id assistant --source-id telegram_business --owner-user-id <TG_ID человека> --retention-days 365`
   Грабли: внутренний шаг идёт СИСТЕМНЫМ python3 → нужен `python3-psycopg` из apt; ошибка «Unprivileged installer apply failed» чаще всего = невалидный YAML в config.yaml или отсутствующий psycopg.
4. `cd /opt/hermes-passive-secretary && python3 install_retention_timer.py --owner assistant`
5. Активация — строго venv-питоном профиля и от имени пользователя:
   `su assistant -c "cd /opt/hermes-passive-secretary && /home/assistant/.hermes/hermes-agent/venv/bin/python manage_passive_secretary.py activate --owner assistant --hermes-home /home/assistant/.hermes --production --confirm-retention-and-scope --enable-search-tool --enable-media-processing --media-asr-provider openrouter --enable-group-passive --confirm-transcript-policy"`
   (`--owner` — это Linux-пользователь, НЕ Telegram ID. `--media-asr-provider local`, если нет OPENROUTER_API_KEY.)
6. Перезапусти сервис.
7. **Руками человека** (Telethon не используем): Telegram → Настройки → Telegram для бизнеса → Чат-боты → вписать имя его бота → выбрать чаты → **НЕ включать ни одного разрешения** (даже «читать сообщения»!). Любое включённое право = система безопасности отвергнет подключение и архив молча не пойдёт (в логах: «rights exceed the configured passive capture profile»).
8. Проверка: человек пишет кому-нибудь пару сообщений → в БД `select count(*) from passive_secretary.messages` растёт. Ноль при подключённом боте = см. п.7.

## Если что-то пошло не так

Смотри `docs/05-troubleshooting.md` — там симптом → причина → лечение для всех известных граблей. Не изобретай обходы поверх защит Hermes (approvals, allowlist, права файлов) — они там намеренно.
