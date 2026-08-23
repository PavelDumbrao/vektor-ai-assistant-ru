# Hermes Passive Secretary — тиражируемый модуль

Standalone-плагин для одного Hermes-профиля. Один Telegram-бот продолжает
обслуживать личный управляющий DM, а Telegram Business-события уходят по
отдельному receive-only контракту в PostgreSQL. Collector hook не импортирует
Telegram SDK и не получает `Update`, `Bot`, adapter, bot token или методы
отправки. Опциональная отправка вынесена в отдельный, по умолчанию выключенный
action edge и не расширяет права collector.

## Граница ответственности

```text
Telegram Business update
  -> Hermes core: owner allowlist + capability-free DTO v2
  -> Hermes core: atomic JSON + fsync in HERMES_HOME/passive-secretary/inbox
  -> telegram_passive_update(event=<dict>)
  -> plugin: validate digest + tenant_owner_id + configured owner allowlist
  -> PostgreSQL transaction
  -> {"handled": true}
  -> Hermes core: atomic ACK/unlink + directory fsync
```

Если PostgreSQL недоступен, плагин возвращает `handled: false`. Core не удаляет
spool-файл и повторяет доставку при startup/periodic replay. Повтор безопасен:
`archive_events.event_key` идемпотентен. Плагин сам никогда не удаляет core
spool и не создаёт второй polling consumer.

DTO v2 обязан включать в проверяемый core digest следующие поля:

- `schema_version: 2`;
- `transport: telegram`;
- `tenant_owner_id` — установленный core идентификатор владельца Business;
- `update_id`, `kind`, `payload`, `payload_sha256`, `received_at_utc`.

`tenant_id`, `source_id` и `test_run_id` добавляются из приватного
`settings.json`, а не принимаются из сообщения, tool arguments или модели.

## Что сохраняется

PostgreSQL — канонический архив:

- `business_connections` — состояние Business-подключения;
- `messages` — текущее состояние сообщения;
- `message_versions` — предыдущая версия при edit;
- `archive_events` — технический idempotency receipt без тела сообщения.
- `outbound_intents` — короткоживущий журнал подготовленной отправки: hash
  текста, точный routing target и итоговый статус, но не plaintext ответа.

Каждая таблица разделена по `tenant_id`, доверенному `tenant_owner_id`,
`source_id` и `test_run_id`. В production `test_run_id` пустой; для временной
проверки задаётся отдельный ID, например `test_run_example`.

При `deleted_business_messages` остаётся tombstone, но `body`, `caption` и
attachment metadata очищаются и в текущем сообщении, и во всех версиях.
Истечение `retention_days` физически удаляет сообщения и каскадно версии.
Retention запускается ежедневно с jitter до 15 минут. Поздний replay события,
которое уже старше cutoff, может находиться в PostgreSQL до следующего прохода;
это эксплуатационная задержка удаления, а не строгая физическая граница в
секундах. Raw core spool retention-команда не удаляет никогда.

## Поиск и автоконтекст

Плагин регистрирует tool `passive_secretary_search`, но installer по умолчанию
оставляет toolset `passive_secretary` в `agent.disabled_toolsets`:

- одна дата: `date: YYYY-MM-DD`;
- относительная дата владельца: `date: today` или `date: yesterday`; значение
  разрешается сервером в `Europe/Moscow`, а не по предположению модели;
- несколько дат: `dates: [...]`;
- диапазон: inclusive `start_date`/`end_date`;
- optional literal `query` и `limit` (не более 200 записей и 40 000 символов
  tool result).

Большая выборка возвращается постранично без `OFFSET`: `has_more` и
`next_cursor` позволяют Hermes запросить продолжение после последней реально
показанной записи. Cursor подписан, привязан к владельцу, источнику, датам и
поисковому запросу и не раскрывает Telegram IDs. Для вложений возвращаются
только безопасные метаданные (`kind`, имя и MIME файла, размер, длительность и
размеры кадра); `file_id`, `file_unique_id` и routing IDs наружу не выдаются.
Ответы и альбомы связаны непрозрачными `reply_to_message_ref` и
`media_group_ref`.

Один вызов ограничен 100 выбранными календарными днями.

Локальные даты считаются в `Europe/Moscow` и переводятся в half-open UTC
интервалы `[start, end)`. Это исключает двойной учёт границы суток. Возвращаются
opaque `source_ref`/`message_ref`, а не пригодный для прямого действия
`chat_id`.

Каждый результат также несёт текущее московское время и статус выбранной даты
(`past`, `today`, `future`). Hermes обязан разрешать слова «сегодня», «завтра»,
«в понедельник» относительно времени конкретного сообщения, отделять уже
завершённое и просроченное от предстоящего и не объявлять действие выполненным
без подтверждения в архиве. В финале ответа он предлагает четыре секции:
фактическую сводку, статусы по срокам, варианты действий и предлагаемые
напоминания. Это предложения владельцу: напоминания не считаются созданными, а
черновики сообщений не считаются отправленными.

Если Telegram передал username, источник показывается как
`Имя (@username)`; при отсутствии username остаётся безопасная display-метка.

Автоматический 24-часовой контекст также по умолчанию выключен. Только после
отдельного `--enable-auto-context` перед owner turn `pre_llm_call` выбирает
ограниченное число сообщений. Контекст:

- доступен только Telegram-сессии, которую hook связал с allowlisted
  `sender_id`;
- не доверяет `tenant_owner_id` из model tool arguments;
- ограничен по сообщениям, символам и длине одного сообщения;
- обёрнут как `UNTRUSTED_DATA`, HTML-escaped и прямо запрещает исполнять
  инструкции, ссылки, секреты или tool requests из архива;
- эфемерен и не меняет system prompt.

И search result, и автоматический контекст содержат архивный текст и передаются
настроенному LLM-провайдеру. Автоконтекст добавляется как volatile current-turn
данные (`persist: false`), но ответ модели может цитировать их. Результат search
tool и assistant reply сохраняются обычным механизмом Hermes session history
(в текущем Hermes это отдельное SQLite/session-хранилище) и могут жить дольше
PostgreSQL retention. Поэтому обе возможности требуют отдельного
`--confirm-transcript-policy`; PostgreSQL purge не является total erasure.

## Ответ клиенту только после явного подтверждения владельца

Исходящие ответы — отдельная opt-in возможность. Hermes не отвечает клиентам
по найденным задачам, входящим сообщениям, содержимому архива, расписанию или
инструкциям внутри чужого текста без двух последовательных действий владельца.

Сначала текущее сообщение allowlisted владельца в личном управляющем Telegram
DM должно начинаться ровно с одного из префиксов:

```text
ОТПРАВИТЬ: Алексею — «Добрый день! Вернусь с ответом завтра»
ОТВЕТИТЬ: Алексею — «Спасибо, получил»
```

Это проверяет код по исходному сообщению текущего turn, а не модель по смыслу.
Префикс из архива, цитаты, результата tool, старого turn, группового чата или
сообщения другого пользователя разрешения не создаёт. Без такого префикса
Hermes не может даже открыть окно отправки.

Затем владелец отдельно нажимает кнопку «Отправить один раз» под точным
получателем и финальным текстом. До этого нажатия вызова отправки нет. Ранее
выданные session/permanent approvals, YOLO-режим и подтверждение другой
отправки не подходят. Отказ, timeout, смена аргументов или повтор операции
закрывают её без отправки.

Перед единственным вызовом Telegram Bot API action edge заново проверяет:

- три локальных выключателя одновременно включены: `outbound_replies_enabled`,
  `business_reply_enabled` и toolset `passive_secretary_outbound`;
- Business connection принадлежит настроенному владельцу и активен;
- Telegram выдал ровно `can_reply=true`, а все остальные action rights
  остались `false`;
- в выбранном личном Business-чате есть подходящее входящее сообщение не старше
  24 часов;
- server-generated `intent_id`, `source_ref`, owner session и hash текста
  совпадают с подготовленным неистёкшим intent.

Telegram-разрешение `can_reply` владелец включает вручную в настройках
Telegram Business. Lifecycle-скрипт не может и не пытается выдать его через
API. Все остальные права на сообщения, профиль, Stories, Gifts и Stars должны
оставаться выключенными.

Текст команды, preview подтверждения, tool result и ответ ассистента могут
сохраниться в Telegram, Hermes session/SQLite transcript и у настроенного
LLM-провайдера дольше PostgreSQL retention. Поэтому outbound требует и
`--confirm-outbound-policy`, и обычный `--confirm-transcript-policy`.
`outbound_intents` не хранит plaintext, но хранит scoped routing IDs и hash до
retention/purge. Если сеть оборвалась после начала Bot API вызова и результат
неизвестен, intent получает статус `ambiguous`: автоматического повтора нет,
чтобы не отправить дубликат.

## Установка в безопасном выключенном состоянии

Установщик требует явную политику хранения. Silent default отсутствует. Release
bundle сначала размещается строго в `/opt/hermes-passive-secretary`: владелец
`root:root`, каталоги не writable для group/other, исходники regular files без
symlink. Bundle содержит module files и frozen core patch tree
`hermes-core-patch/`; installers проверяют владельца и зафиксированные SHA-256.

```bash
BUNDLE=/opt/hermes-passive-secretary

sudo /usr/bin/python3 "$BUNDLE/install_core_patch.py" \
  --source-repo "$BUNDLE/hermes-core-patch" \
  --target-repo /home/<client>/.hermes/hermes-agent \
  --owner <client>

sudo /usr/bin/python3 "$BUNDLE/install_passive_secretary.py" install \
  --client <client> \
  --owner <client> \
  --hermes-home /home/<client>/.hermes \
  --tenant-id <client_tenant> \
  --source-id telegram_business \
  --owner-user-id <telegram_numeric_owner_id> \
  --retention-days <explicit_days>
```

Root-процесс только валидирует trusted bundle и canonical owner layout. Все
backup/dependency/plugin/core writes выполняются в дочернем процессе после
необратимого drop до UID/GID владельца с пустыми supplementary groups; owner
venv Python никогда не запускается от root.

По умолчанию installer использует `<HERMES_HOME>/bin/uv` и ставит в Hermes
venv зафиксированную зависимость `psycopg[binary]==3.3.4`. `--skip-deps`
предназначен только для offline image build.

Установка файлов транзакционна в границах `config.yaml` и каталога плагина:

1. dependency install полностью завершается до любых изменений config/plugin;
2. создаётся приватный backup исходного `config.yaml` и полного существующего
   `plugins/passive-secretary/`;
3. новый plugin и config записываются только после успешного backup;
4. при ошибке исходные config/plugin восстанавливаются; если плагина раньше не
   было, удаляется только точный новый `plugins/passive-secretary/`, соседние
   плагины не затрагиваются.

Путь backup выводится установщиком. Повторный запуск идемпотентен: запись в
`plugins.enabled` не дублируется, а перед заменой снова сохраняется актуальная
версия плагина.

После установки гарантированно остаются независимые выключатели capture,
поиска и исходящих действий:

```yaml
platforms.telegram.extra.business_updates_mode: blocked
platforms.telegram.extra.business_reply_enabled: false
platforms.telegram.extra.passive_media_enabled: false
platforms.telegram.extra.group_passive_enabled: false
platforms.telegram.extra.group_passive_chat_ids: []
```

```json
{
  "capture_enabled": false,
  "auto_context_enabled": false,
  "outbound_replies_enabled": false
}
```

```yaml
agent.disabled_toolsets: [..., passive_secretary, passive_secretary_outbound]
```

Installer также записывает обязательный
`platforms.telegram.extra.business_owner_ids`. Режим `blocked` прекращает
Business update до обычного assistant dispatch, но ничего не сохраняет. Режим
`off` означает legacy fall-through и для подготовленного клиента не подходит.
Plugin загружается, но capture до отдельной активации не начинается.

## Ежедневный retention timer

Для каждого клиента timer устанавливается отдельной root-командой из того же
trusted `/opt` bundle:

```bash
sudo /usr/bin/python3 \
  /opt/hermes-passive-secretary/install_retention_timer.py \
  --owner <client>
```

Installer создаёт детерминированную пару
`<client>-hermes-passive-secretary-retention.service/.timer`. Oneshot работает
от UID/GID клиента, запускает только его Hermes venv с `python -B -E -s`, exact
`settings.json` и private `.env`; systemd sandbox запрещает лишние capabilities
и запись в system tree.

Весь snapshot/write/smoke/enable/rollback сериализован глобальным root-only
non-blocking lock. Сначала installer атомарно пишет units, выполняет
`daemon-reload`, проверяет загруженные definitions без drop-ins и запускает
oneshot smoke. Только после успешного smoke timer включается через
`enable --now`; при ошибке исходные units и прежнее enabled/active state
восстанавливаются. Повторный запуск идемпотентен. Timer имеет
`OnCalendar=daily`, `Persistent=true` и jitter до 15 минут.

## Операторский lifecycle

`manage_passive_secretary.py` выполняется от UID владельца профиля, принимает только
канонический `<home владельца>/.hermes` и отклоняет symlink для Hermes home,
plugin, `config.yaml`, `settings.json` и `.env`. Скрипт не перезапускает Hermes:
изменённые файлы — это desired state, который вступает в силу после штатного
restart gateway. Успешные `activate` и `deactivate` поэтому всегда выводят
`restart_required=true`, включая идемпотентный повтор.

Secrets читаются как данные из private `.env`, без shell `source`. Если
одноимённая process environment переменная задана другим значением, mutation
отклоняется, чтобы оператор случайно не проверил и не очистил другую БД.

```bash
OWNER_PY=/home/<client>/.hermes/hermes-agent/venv/bin/python
MANAGER=/opt/hermes-passive-secretary/manage_passive_secretary.py
```

Безопасный статус (только booleans/counts, без DSN и значений ключей):

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" status \
  --owner <client> \
  --hermes-home /home/<client>/.hermes
```

`status` также показывает число/размер pending spool-файлов и
`spool_oldest_age_seconds`, не читая их содержимое. Raw pending spool никогда
не удаляется retention-задачей; возраст старше пяти минут требует проверки
оператором причины replay/DB failure.

Тестовая активация требует точного непустого `test_run_id` из установленного
`settings.json`. До создания backup и записи файлов оператор проверяет mode,
plugin, точное совпадение owner allowlist, private `.env` mode `0600`, наличие
обоих секретов, импорт `psycopg` и реальный `ensure_schema()` PostgreSQL:

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" activate \
  --owner <client> \
  --hermes-home /home/<client>/.hermes \
  --test-run-id <exact_test_run_id>
```

Эта команда включает только receive-only capture. Search tool, автоматический
24-часовой контекст, media processing и исходящие ответы остаются выключены.
Любая model-доступная
возможность включается только явно и требует отдельного подтверждения политики
provider/session transcript, например:

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" activate \
  --owner <client> \
  --hermes-home /home/<client>/.hermes \
  --test-run-id <exact_test_run_id> \
  --enable-search-tool \
  --enable-auto-context \
  --confirm-transcript-policy
```

Для отдельного теста исходящих ответов используются оба явных подтверждения:

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" activate \
  --owner <client> \
  --hermes-home /home/<client>/.hermes \
  --test-run-id <exact_test_run_id> \
  --enable-search-tool \
  --enable-outbound-replies \
  --confirm-outbound-policy \
  --confirm-transcript-policy
```

Команда синхронно включает три локальных outbound gate. После штатного restart
владелец вручную включает в Telegram Business ровно `can_reply`; другие action
rights остаются выключены. Каждая фактическая отправка всё равно требует нового
one-shot подтверждения владельца в управляющем DM.

Порядок активации: `capture_enabled=true`, затем
`business_updates_mode=passive`. Оба исходных файла сохраняются в private
backup; ошибка второго шага восстанавливает оба. Повторная команда не создаёт
лишний backup, но снова выполняет DB preflight.

### Обработка голосовых и media

Media enrichment по умолчанию выключен (`passive_media_enabled: false`) и не
меняет основной сбор сообщений. Его можно включить только явно, добавив к
`activate` флаги `--enable-media-processing --media-asr-provider openrouter`.
До активации приватный `/home/<client>/.hermes/.env` должен содержать
`OPENROUTER_API_KEY`; значение нельзя передавать в argv, логи или чат. Worker
использует фиксированные endpoint `https://openrouter.ai/api/v1/audio/transcriptions`
и модель `openai/whisper-large-v3-turbo`. Произвольный base URL или model из
конфига не принимаются. Worker использует тот же уже
запущенный Telegram Bot API client; второй `getUpdates`/polling-процесс не
создаётся.

Обычные голосовые, отправленные непосредственно Hermes, используют тот же ключ
и ту же модель при конфигурации:

```yaml
stt:
  enabled: true
  provider: openrouter_whisper
  language: ru
  openrouter:
    model: openai/whisper-large-v3-turbo
```

Для `openrouter_whisper` автоматический локальный fallback намеренно выключен:
ошибка ключа, баланса, сети или provider возвращает явную ошибку, а не незаметно
меняет движок. Raw audio при такой настройке передаётся OpenRouter и выбранному
им upstream provider; это отдельная граница приватности и биллинга.

В PostgreSQL сохраняются ограниченные metadata и результат enrichment.
Транскрипт создаётся только для Telegram `voice` и `video_note`, причём тип
сохраняется соответственно как `voice`/`голосовое сообщение` или
`video_note`/`видеокружок`, чтобы Hermes явно различал эти форматы. Пустой
результат VAD один раз повторяется без VAD. Для `video_note` audio-дорожка
сначала извлекается локальным `ffmpeg` в private mono WAV, потому что прямая
передача Telegram MP4 может дать пустой ответ Whisper; временный WAV удаляется
после результата. Если речи действительно не удалось
распознать, сохраняется явная отметка `[речь не распознана]`. Любой `document`,
включая аудиофайл, сохраняется только
как metadata и не ставится в очередь ASR. Telegram `file_id`, bot token,
временный файл и URL скачивания не должны попадать в результат enrichment.
Перед включением оператор обязан проверить валидность отдельного budget-limited
OpenRouter key, доступность модели, максимальный размер/duration, баланс и
свободное временное место. Локальный provider остаётся только как явный rollback
(`passive_media_asr_provider: local`), но не включается автоматически.
Успешный logical purge/retention не означает гарантированное total erasure:
отдельный lifecycle имеют WAL/backups, внешние логи и уже созданные Hermes/LLM
transcripts.

Для media, уже попавших в БД до включения worker, устанавливается отдельная
root-owned operator-only утилита `/opt/hermes-passive-secretary/legacy_media_seed.py`.
Она по умолчанию делает dry-run, не обращается к Telegram/ASR и создаёт private
jobs только с явным `--apply`. Утилита не устанавливается в importable Hermes
plugin package и не является model tool.

### Ограниченный импорт истории

`/opt/hermes-passive-secretary/history_backfill.py` — отдельная root-owned
operator-only команда, а не часть Hermes plugin. Она использует единственный уже
запущенный Telegram API Engine, импортирует только личные текстовые сообщения в
точный test scope и по умолчанию выполняет dry-run. Saved Messages и точный
управляющий bot исключаются по идентичности, а импортированные строки получают
`ingest_origin=history_backfill` и никогда не могут служить 24-часовым anchor для
Business reply.

Для уже работающего Business-архива безопасный режим —
`--known-archive-chats`. Он получает список источников внутренним read-only
запросом к `passive_secretary.messages`, строго связанным с tenant, owner,
source и test-run из валидированных settings/CLI scope. Для каждого `chat_id`
берётся последняя сохранённая метка. Raw идентификаторы клиентских чатов не
передаются в argv и не попадают в агрегатный stdout. Режим вообще не вызывает
`/api/chats`, поэтому число посторонних Telegram-диалогов и ограничение этого
endpoint не делают выборку частичной.

Оператор сначала через root-owned secret loader передаёт только четыре точных
значения: tenant DSN, source-ref key, API Engine key и текущий bot token. Их нельзя
`source`-ить, печатать или помещать в argv. После такой инъекции dry-run и commit
выглядят одинаково, кроме последнего флага:

```bash
OPERATOR_PY=/opt/hermes-passive-secretary/operator-venv/bin/python3
BACKFILL=/opt/hermes-passive-secretary/history_backfill.py

"$OPERATOR_PY" -I -B -c \
  'import runpy,sys; sys.path.insert(0,"/opt/hermes-passive-secretary"); runpy.run_path(sys.argv.pop(1),run_name="__main__")' \
  "$BACKFILL" \
  --settings /home/<client>/.hermes/plugins/passive-secretary/settings.json \
  --owner-id <owner_telegram_id> \
  --test-run-id <exact_test_run_id> \
  --start <UTC_ISO_START> \
  --end-exclusive <UTC_ISO_END> \
  --known-archive-chats \
  --max-dialogs 1000 \
  --page-size 100 \
  --max-wall-seconds 180 \
  --dry-run

# Только после проверки агрегатного dry-run:
# заменить --dry-run на --commit и обернуть процесс внешним timeout.
```

Ограничения known-archive режима:

- он дополняет историю только тех чатов, где в точном архивном scope уже есть
  хотя бы одна строка `ingest_origin=business_update`; ранее импортированные
  строки не расширяют следующий known-chat scope, а новые/ещё не замеченные
  Business-чаты режим не обнаруживает;
- пустая выборка, несовпадение owner/test scope, ошибка/timeout PostgreSQL,
  некорректный или превышающий `--max-dialogs` набор источников завершают запуск
  до export и insert;
- owner Saved Messages и точный управляющий bot исключаются повторно после DB
  selection; если после исключения источников нет, запуск завершается ошибкой;
- общий лимит `--max-messages`, лимит страниц на чат, body budget, wall clock и
  максимум 24 часа остаются обязательными; сначала строится полный ограниченный
  план, затем commit выполняет прежний insert-only `ON CONFLICT DO NOTHING`;
- импорт восстанавливает только текущее текстовое представление, но не
  исторические edit/delete/media-события и не создаёт outbound connection/anchor.

Legacy/default режим сканирования всех личных диалогов сохранён для обратной
совместимости и может быть указан явно как `--all-private-chats`. Эти два флага
взаимоисключающие. All-private режим зависит от непагинируемого `/api/chats` и
fail-closed отказывается продолжать, если ответ достигает `--max-dialogs`.

Текущий API Engine использует GET route с raw `chat_id`; Uvicorn access log
поэтому сохраняет эти идентификаторы в root-only `/tmp/telegram-api.log` mode
0600. Это ограниченное операционное исключение, а не обещание zero-ID logging.
Тела сообщений, токены и DSN команда в stdout/stderr не выводит.

Production-активация допускается только при пустом `test_run_id`, двумя
явными подтверждениями — `--production` и
`--confirm-retention-and-scope` — и успешном live preflight установленного
retention timer/service. Lifecycle проверяет точные детерминированные unit
names, root-owned `FragmentPath`, отсутствие drop-ins, exact owner UID/GID,
`ExecStart`, `WorkingDirectory`, settings/EnvironmentFile, успешный предыдущий
oneshot smoke и состояние timer `enabled + active/waiting`:

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" activate \
  --owner <client> \
  --hermes-home /home/<client>/.hermes \
  --production \
  --confirm-retention-and-scope
```

Аварийное выключение не зависит от PostgreSQL и секретов:

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" deactivate \
  --owner <client> \
  --hermes-home /home/<client>/.hermes
```

Порядок выключения: сначала `business_updates_mode=blocked`, затем отдельной
атомарной записью best-effort выставляются `business_reply_enabled=false`,
`passive_media_enabled=false` и
выключаются toolsets `passive_secretary`/`passive_secretary_outbound`, затем в
settings выключаются `capture_enabled`, `auto_context_enabled` и
`outbound_replies_enabled`. Отсутствующий или повреждённый `settings.json` не
может помешать первому атомарному transport block. Если последующие writes
упали, transport намеренно остаётся blocked, rollback обратно в passive не
выполняется, а результат явно сообщает, какие defense-in-depth выключатели не
удалось обновить.

Тестовый scope удаляется только после deactivation, остановки точного
детерминированного gateway unit и проверки пустого core inbox:

```bash
sudo -u <client> -- "$OWNER_PY" -B -E -s "$MANAGER" purge-test \
  --owner <client> \
  --hermes-home /home/<client>/.hermes \
  --test-run-id <exact_test_run_id> \
  --service <client>-hermes.service \
  --confirm-service-stopped
```

`purge-test` отказывается работать с пустым production scope, активным или
смешанным state, несовпадающим owner allowlist, другим `test_run_id`, pending
spool, media processing не в literal `false` или небезопасным spool path.
Tenant/source/owners/test-run берутся только
из валидированного `settings.json`, а не из SQL/CLI аргументов. Одна bounded
PostgreSQL transaction сначала берёт детерминированный
`SHARE ROW EXCLUSIVE` lock на все шесть scoped-таблиц, затем удаляет
`archive_events`, connections, media enrichments, `outbound_intents` и messages; message versions
удаляются каскадом. До commit все шесть scoped counts должны быть нулевыми.
Вывод содержит только количества, не содержимое и не секреты.

Почему обязательны restart/stop и пустой inbox: raw core DTO ещё не содержит
`test_run_id`; plugin добавляет текущий scope при replay. Старый pending event
иначе может снова наполнить тестовую область или попасть в следующий scope.
Logical purge не стирает raw pending spool, Hermes SQLite/session tool и
assistant rows, логи внешнего LLM-провайдера, уже существующие PostgreSQL
backups/WAL или другие резервные копии. Их lifecycle управляется отдельной
согласованной retention-политикой. `purge-test` удаляет только точный
PostgreSQL test scope и не обещает total erasure.

### Доступность toolset в Telegram

После установки standalone plugin загружается для receive-only capture, но
toolset `passive_secretary` остаётся в `agent.disabled_toolsets`. Lifecycle
удаляет его оттуда только при явном `activate --enable-search-tool
--confirm-transcript-policy`. `deactivate` best-effort возвращает toolset в
disabled state. Поведенческие integration tests проверяют реальные
`PluginManager`, platform tools и registry schema.

Отдельный toolset `passive_secretary_outbound` после установки также disabled.
Он включается только комбинацией `activate --enable-outbound-replies
--confirm-outbound-policy --confirm-transcript-policy`; одновременно lifecycle
выставляет settings/config gates и в `status` показывает все три значения и их
согласованность. Смешанное состояние не считается ни корректно active, ни
полностью disabled. `purge-test` требует literal false для обоих outbound
флагов и disabled outbound toolset.

## Секреты и PostgreSQL

Секретные значения хранятся только в `<HERMES_HOME>/.env` mode `0600` или в
эквивалентном secret manager:

```dotenv
PASSIVE_SECRETARY_DATABASE_URL=<tenant-only PostgreSQL DSN>
PASSIVE_SECRETARY_SOURCE_REF_KEY=<at least 32 random characters>
```

В `config.yaml` и `settings.json` находятся только имена env vars. PostgreSQL
роль должна иметь доступ только к БД этого клиента. Для выделенного контейнера
задайте memory/CPU limit и private network или Unix socket; публичный Postgres
порт не нужен.

## Отдельная активация после проверки

Активация намеренно не входит в install step. Перед ней оператор проверяет:

1. владелец письменно согласовал категории Business-чатов и retention;
2. `business_owner_ids` совпадает с реальным Telegram owner ID;
3. PostgreSQL доступен из Hermes-процесса, schema создаётся, роль tenant-only;
4. `.env` mode `0600`, оба секрета присутствуют, значения не выводятся;
5. offline tests зелёные;
6. сначала задан отдельный `test_run_id` и один согласованный тестовый чат;
7. для receive-only проверки все Telegram Business action rights выключены;
   для отдельного outbound-теста вручную включён ровно `can_reply`, остальные
   права выключены;
8. для production retention service прошёл smoke, а timer реально
   `enabled + active/waiting` и соответствует exact unit identity;
9. если включаются search/auto-context/outbound, согласованы LLM-provider
   retention и Hermes session/SQLite transcript retention, а оператор передал
   отдельный `--confirm-transcript-policy`; outbound дополнительно требует
   `--confirm-outbound-policy`.

Только после этого в одном change window lifecycle выставляет запрошенные
settings/config/toolset flags и `business_updates_mode: passive`, затем
выполняется один штатный restart gateway. При rollback сначала вернуть
`business_updates_mode: blocked`, затем выключить reply/tool/settings gates и
перезапустить.

## Изолированный runtime

Раздел про операторский пилотный runtime намеренно удалён из публичной версии: он описывал разовую внутреннюю процедуру и не нужен для установки. Все необходимые шаги установки — в AGENTS.md корня репозитория (раздел «Пассивный секретарь»).

## Offline-проверка

Никакие реальные Telegram/DB credentials или сеть тестам не нужны:

```bash
cd deployment/passive-secretary-module
python3 -m py_compile \
  telegram_pilot.py build_telegram_pilot_runtime.py \
  tests/test_telegram_pilot.py tests/test_build_telegram_pilot_runtime.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest \
  tests/test_telegram_pilot.py tests/test_build_telegram_pilot_runtime.py

# Полный module-suite требует dev dependencies старых модулей
# (python-dotenv/psycopg); этот interpreter не используется pilot runtime.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /home/<client>/.hermes/hermes-agent/venv/bin/python \
  -m unittest discover -s tests -p 'test_*.py'
```

Покрыты DTO digest/owner gates, ACK только после commit, отказ БД с сохранением
core replay, session-derived tool scope, 24h untrusted context, Moscow
half-open ranges, tombstones/body purge, installer disabled posture и
обязательный `retention_days`, backup/rollback/idempotence установщика,
lifecycle ordering/rollback/fail-closed purge, durable no-ACK replay,
late-ACK exact-ID recovery, inter-unlink finalize drift, production retention-unit
preflight, explicit transcript consent, retention timer smoke/rollback/flock и
реальный plugin toolset/schema resolution.

## Что не входит в этот slice

- production deploy/restart;
- подключение реального Telegram Business аккаунта;
- OCR фотографий и документов;
- исторический backfill edit/delete/media-событий: operator import сохраняет
  только доступный текст, а legacy voice/video_note обрабатывает отдельный seed;

### Тихий архив обычных групп

Групповой режим включается для клиента отдельным флагом активации:

```bash
... manage_passive_secretary.py activate ... --enable-group-passive
```

После этого владелец добавляет своего бота в группу. До согласия ни одно
сообщение группы не попадает в архив. Бот отправляет только владельцу в личный
чат карточку с названием группы и кнопками `✅ Да, подключить` / `❌ Нет`.
Подтверждение одноразовое и действует только на точную группу; callback не
содержит raw group ID. Нажать кнопку может только configured owner в своём
private DM. При `Нет`, просроченном запросе или добавлении бота другим
участником capture остаётся закрытым; при чужом добавлении и явном отказе бот
выходит из группы.

Одобренные группы хранятся в приватном fsync-backed реестре внутри
`HERMES_HOME`. Статический список ниже сохранён только как migration/bootstrap
совместимость; новая pending/denied запись всегда перекрывает старый allowlist:

```yaml
platforms:
  telegram:
    extra:
      group_passive_enabled: true
      group_passive_chat_ids: []
```

После нажатия `Да` любое новое обычное групповое сообщение сначала проходит durable
spool, а затем сохраняется тем же PostgreSQL collector. Разрешённая группа не
попадает в обычный Hermes dialogue route, поэтому бот не отвечает, не ставит
реакции и не выполняет команды внутри группы. Сообщения из остальных групп
также молча отбрасываются до agent handler.

Сохраняются текст, подпись, дата, название и публичный `@username` группы,
имя/`@username` отправителя, reply/media-group relations, edit и безопасные
metadata вложений. Telegram Bot API не присылает обычному боту универсальные
события удаления сообщений в группе, поэтому удаления гарантированно
отслеживаются только для Telegram Business чатов. `voice` и `video_note`
транскрибируются при включённом media pipeline; документы остаются
metadata-only. Protected-content сообщения никогда не скачиваются.

Чтобы бот без прав администратора видел все сообщения, в BotFather должен быть
выключен Privacy Mode, после чего бота нужно удалить из группы и добавить снова.
Изменение глобально для бота, поэтому owner-consent реестр обязателен. Для
реальных групп заранее уведомите участников об архивировании и распознавании
голоса.
