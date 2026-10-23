# Hermes Forge Bot Manager

Central Telegram entry/control plane for `@ProAIHermesBot` (`Hermes Forge | Pro AI`). The Forge bot already exists. Clients use it to create their own Telegram Managed Bot; that child bot remains owned by the client and becomes the Telegram identity of their Hermes.

## Current hiring flow

1. Authorized user opens `@ProAIHermesBot` and chooses **Нанять AI-ассистента**.
2. User enters display name and bot username directly in chat.
3. Forge validates/canonicalizes the username and opens Telegram's native managed-bot confirmation.
4. Telegram creates the client-owned child bot and sends Forge a `managed_bot` update.
5. Forge retrieves the child token into root-only managed state and applies restricted access.
6. If Telegram identity already maps to an existing Hermes profile, the token is installed using the legacy safe bind path.
7. Otherwise Forge writes a versioned `HermesInstance` desired-state record and starts the bounded provisioner systemd unit.
8. The provisioner creates/reconciles the Linux tenant, shared runtime binding, PostgreSQL tenant, plugins and service.
9. Forge reports `active` only after runtime/Telegram health succeeds.

The normal new-client lifecycle is `provisioning -> active` or explicit `provision_failed`; `awaiting_profile` is no longer the intended product path.
## Runtime components

- manager: `/opt/proai-hermes-manager/manager.py`;
- manager secret: `/etc/proai-hermes-manager.env`, root `0600`;
- platform secrets: `/etc/proai-hermes-platform.env`, root `0600`;
- desired state: `/opt/proai-hermes-manager/state/instances/*.json`, root `0600`;
- child bot tokens: `/opt/proai-hermes-manager/state/managed/<bot_id>.env`, root `0600`;
- provisioning receipts: `/opt/proai-hermes-manager/state/provisioning/*.json`, root `0600`;
- manager service: `proai-hermes-manager.service`;
- bounded worker: `proai-hermes-provisioner@.service`.

The manager is intentionally sandboxed with `ProtectSystem=full`. It does not perform Linux-user/systemd/database mutations itself. It writes desired state and asks systemd to start a validated provisioner instance. The provisioner has no arbitrary-command API.

`seed_platform_secrets.py` is a one-time migration helper for the current VPS. It copies only approved provider keys into the dedicated platform file and never prints values.

## Existing assistant imports

Bots that existed before Managed Bots can be registered in Forge without recreation.
Pavel can run `/importprofile <linux_owner>` in a private Forge chat. The manager
resolves the profile owner, reads the existing bot identity, and stores only safe
metadata under `state.imported`; the existing bot token is never copied into state.
Imported assistants appear in **Мои AI-ассистенты** with live systemd status.
Telegram-level managed-bot privileges are not retroactively added: those are available
only for bots originally created through Telegram's managed-bot flow.

## Internationalization

Forge keeps client-facing copy outside the control-plane logic in `i18n.py`.
The current locale set contains 25 languages: Russian, English, Spanish, German, French,
Portuguese, Chinese, Arabic, Hindi, Turkish, Italian, Japanese, Korean, Indonesian,
Vietnamese, Polish, Ukrainian, Dutch, Persian, Hebrew, Thai, Bengali, Urdu, Malay, and Filipino.

- On first interaction Forge reads Telegram `user.language_code`.
- A per-user choice is stored in `state.locales` and wins over auto-detection.
- `🌐 Language` and `/language` expose the manual selector.
- Unknown explicit Telegram locales fall back to English.
- Legacy users with no language code retain Russian behavior.
- Missing translated strings fall back to English rather than breaking a flow.
- Adding a language requires a catalog entry, not new handlers or provisioning logic.

Private profile data, memories, tokens, and tenant boundaries are independent
of UI locale; changing language changes presentation only.

## Living Memory is included for every new client

The bounded provisioner installs Living Memory v0.1 automatically. It is a platform default, not an optional capability toggle. No client needs a separate memory setup step.

Creation order:
1. Validate platform primary and distinct OpenRouter reserve credentials before creating the Linux account.
2. Render only the generic tenant template; do not clone any existing client's memory.
3. Install the root-owned Living Memory release with that tenant's private storage. Keep its timer stopped during preparation.
4. Keep built-in memory reading and write approval enabled; set `memory.nudge_interval=0` to avoid competing per-turn background learning.
5. Run owner-scoped configuration checks and synthetic primary/reserve model probes, with no real conversation sent.
6. Start Hermes and require current systemd/gateway PID agreement, running gateway and connected Telegram.
7. Enable and verify the daily Living Memory timer, then publish `active` with a `health.living_memory` receipt.

A failed model, gateway or timer check must not publish `active`. Scoped cleanup stops only the new tenant's memory timer/service. Retrying provisioning preserves existing user memory and uses the same installed release.

Default schedule is 04:00 Europe/Moscow with up to 30 minutes of per-profile delay. Review scope remains only direct Telegram conversations between this owner and Hermes. GPT-5.6 Sol uses high reasoning; reserve is GPT-5.6 Sol through OpenRouter with a different credential. Missing reserve access blocks onboarding readiness rather than silently creating a reduced-quality memory agent.

The installer vendors `modules/living-memory` and `living_memory_onboarding.py`. Deploying this factory change does not reinstall, restart or migrate existing tenants.

Verification: automated factory/negative-path tests are part of Server snapshot CI. An isolated Linux canary additionally exercises the real installer, both provider routes, systemd timer/readiness, sandboxed empty scan and cross-tenant read denials without creating a public Telegram bot.

## Key Broker lane allocation for new clients

New tenants receive a dedicated localhost key broker lane instead of platform secrets.
The provisioner requests a lane via `/usr/local/sbin/lanectl add --client <owner> --elevenlabs-chars 15000 --rpm 60`
and reads the assigned port (`1024 <= port <= 65535`) from `/etc/proai-keybroker/lanes.json`.

- The profile's `config.yaml` routes `model.base_url`, `providers.*.api`, `fallback_providers[].base_url`,
  `tts.elevenlabs.base_url`, and `mcp_servers.proaisaver.url` to `http://127.0.0.1:<broker_port>/...`.
- In `.env`, platform secrets are replaced with non-secret placeholder tokens (`keybroker-*`),
  leaving only `TAVILY_API_KEY` as a direct passthrough. Endpoint URLs are provided via
  `GRSAI_BASE_URL`, `LINGSUAN_IMAGE_BASE_URL`, `LIVING_MEMORY_FALLBACK_BASE`, and
  `HERMES_OPENROUTER_STT_ENDPOINT`.
- `mcp_servers.proaisaver` uses `Authorization: Bearer keybroker-saver` and is preserved without token conflict.

## Админ-команды и восстановление

Команды управления доступны только администратору (`PAVEL_ID`):

- `/tenants` — сводная таблица тенантов Forge (Linux-owner, bot username, Telegram identity владельца `@username (Имя)`, текущий статус, номер попытки найма, дата обновления, последняя ошибка).
- `/failed` — фильтр сбойных записей со статусами `provision_failed` и `token_pending` с причинами сбоя и указанием клиента.
- `/requests` — список ожидающих заявок на доступ (`@username — имя, id, когда`).
- `/retry <bot_id|tg_id>` — повторный запуск найма: для `token_pending` повторяет безопасное получение токена от Telegram; для `provision_failed` увеличивает `attempt`, обновляет `provision_started_at` и запускает unit провижинера с актуальным `FORGE_STABLE_RELEASE_ID`.
- `/offboard <tg_id>` — вывод клиента из эксплуатации в два шага (второй шаг: `/offboard <tg_id> confirm` в течение 5 минут). Менеджер останавливает и отключает systemd-сервисы и таймеры (`<owner>-hermes`, retention-таймер пассивного секретаря, экспорт shared-metrics), удаляет lane брокера (`lanectl remove`), переносит токен, instance-файл и receipt в `state/offboarded/<ts>-<owner>/` (0700 root), записывает `offboarded_at` и срок хранения 30 дней (`purge_after`), уведомляет владельца с датой безвозвратного удаления и выводит администратору памятку с возможностью отмены.
- `/offboard-cancel <tg_id>` — отмена вывода из эксплуатации: снимает отметки `purge_after` и `offboarded_at`, восстанавливает статус `active`, повторно включает systemd-сервисы и таймеры, восстанавливает lane брокера и уведомляет клиента и администратора.
- Замена токена у существующего профиля: менеджер в каталог клиента не пишет; задание ложится в `state/token-updates/<owner>.json` (`pending_operator`), дальше вручную оператором.
- `/allow <tg_id>`, `/deny <tg_id>`, `/allowed` — управление списком авторизованных пользователей; `/deny` заносит в `denied_users` и имеет абсолютный приоритет над любыми записями профилей и ботов.

## Заявки на доступ и идентификация пользователей

- При попытке неавторизованного входа выдаётся сообщение с inline-кнопкой «Запросить доступ».
- Обработка callback `request_access` выполняется до проверки авторизации. Заявка регистрируется в `state.access_requests`, администратор (`PAVEL_ID`) получает уведомление с `@username` (или ссылкой на профиль при отсутствии username) и inline-кнопками `access_allow:<uid>` и `access_deny:<uid>`.
- Повторные запросы ограничены: в течение 24 часов повторное нажатие не дублирует уведомление администратору; после отклонения повтор доступен через 7 дней.
- Во всех сообщениях администратору (`/tenants`, `/failed`, оповещения о найме, health alerts, offboard) используется хелпер `user_label` (`@username (Имя)` или `Имя (без username)`).
- Кэш пользователей хранится в `state.users` и обновляется при каждом взаимодействии.

## Срок хранения данных и отмена offboarding

- Данные отключённого ассистента хранятся 30 дней (`purge_after = offboarded_at + 30*86400`).
- Клиент получает уведомление с точной календарной датой удаления данных.
- Точка интеграции: функция `purge_due(state)` вызывается в `reconcile_provisioning` и фиксирует записи, готовые к окончательной очистке (`purge_due`), к которой подключается процедура удаления.
- До наступления даты зачистки offboard может быть отменён командой `/offboard-cancel <tg_id>`.

## Точки расширения: Оплата и поддержка

- В главном меню добавлены кнопки «Оплата» (`pay`) и «Поддержка» (`support`).
- Обработчики ведут в функции-заглушки `open_pay` и `open_support`, информирующие о скором запуске сервисов.
- В `handle_message` предусмотрен хук `support_message` для маршрутизации сообщений пользователей в модуль поддержки.

### Целостность и восстановление состояния

- Все операции записи `state.json` создают резервную копию `state.json.bak` перед заменой и выполняют `fsync` для файла и директории `state/`.
- При ошибке разбора `state.json` повреждённый файл изолируется в `state.json.corrupt-<ts>`, после чего менеджер восстанавливается из `state.json.bak`. Старт с пустым состоянием при отсутствии резервной копии возможен только при явном флаге `FORGE_MANAGER_ALLOW_EMPTY_STATE=1`.
