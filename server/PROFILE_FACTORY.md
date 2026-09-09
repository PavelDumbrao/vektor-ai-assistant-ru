# Hermes Profile Factory

Цель: новый клиент получает тот же проверенный продуктовый слой без ручного копирования случайного состояния с VPS.

## Source of truth

GitHub хранит только воспроизводимый код и sanitized templates. Секреты, реальные Telegram IDs, SOUL/USER клиента, архив сообщений и OAuth остаются private runtime state.

Базовый профиль состоит из:
- pinned immutable Hermes/Vektor runtime;
- отдельного Linux user и `HERMES_HOME`;
- отдельного systemd process;
- отдельной PostgreSQL tenant database;
- Passive Secretary + full-history recall;
- optional Maton onboarding;
- индивидуальных `SOUL.md`, `USER.md`, workspace и разрешений.

`server/fleet.json` фиксирует установленные профили и variant/release, но не является хранилищем секретов.
## Provisioning flow

1. Создать Linux user/home/workspace и private temp directory.
2. Provision отдельную PostgreSQL DB/role через `modules/passive-secretary-postgres`.
3. Выбрать pinned release из GitHub и подключить immutable shared runtime.
4. Развернуть sanitized profile template, затем локально подставить owner/admin IDs и env secrets.
5. Установить стандартные plugins из GitHub, включая Passive Secretary recall и безопасный shared `video-editor` toolset; тяжёлый video runtime остаётся root-owned и общий для профилей. Для `video-editor` обязательны оба profile-step: `python3 modules/video-editor/install.py --owner <owner>` и `python3 modules/video-editor/configure_asr_client.py --owner <owner>`. Второй шаг создаёт отдельный broker token; OpenRouter secret в client home не копируется.
6. Если профиль должен принимать Telegram-файлы больше public Bot API limit, установить host package `acl`, проверить existing local `telegram-bot-api --local` и выполнить `python3 modules/telegram-large-file/install.py --owner <owner> --apply`. Installer выдаёт только tenant-local ACL + read-only bind mount; Linux user нельзя добавлять в общую `telegram-transcriber` group, а bot token нельзя переносить в unit/template.
7. Проверить от имени клиента `runtime_ready=true` и authenticated `broker_available=true`; `auto` должен использовать shared OpenRouter broker, а local whisper.cpp остаётся fallback.
8. Создать персональные SOUL/USER только из подтверждённых данных клиента.
9. Для клиентского Telegram-бота включить в BotFather `Secretary Mode`; до owner consent профиль остаётся `blocked`.
10. После подключения владельцем перевести Business capture только в receive-only: `business_updates_mode: passive`, `passive_media_enabled: true`, `business_reply_enabled: false`, а toolset `passive_secretary_outbound` должен оставаться disabled.
11. Владелец сам выбирает scope Telegram Business: все приватные чаты с исключениями или только выбранные. Это нельзя подменять серверной настройкой.
12. Подключать Telegram-группы через owner consent; trusted technical inviter может только инициировать enrollment.
13. Старую историю импортировать отдельным `history_backfill`, сохраняя provenance.
14. Прогнать E2E: Telegram DM, Business receive-only capture, group capture, history recall, voice ASR, fallback LLM, Telegram video >20 MiB через profile-isolated local ingress и `video_editor_prepare` на полученном cache path.
15. Зафиксировать sanitized snapshot/изменение отдельным PR и дождаться CI.

Никаких изменений общей продуктовой логики только в `/home/<client>/.hermes`: сначала или сразу вслед за пилотом они должны стать модулем/патчем в GitHub.
## Memory/retrieval product layers

Layer 1, production baseline:
- exact-date archive search;
- Russian FTS with stemming;
- trigram fuzzy search for typos;
- source/sender/date/origin filters;
- explicit LIVE vs IMPORTED_HISTORY provenance.

Layer 2, semantic memory:
- move PostgreSQL cluster to a separately reviewed pinned pgvector-capable image;
- tenant-local `message_embeddings` table keyed to message identity + content hash;
- asynchronous batch embeddings for history and incremental embeddings for live events;
- immutable embedding model/version metadata;
- vector + FTS + fuzzy candidate sets combined with Reciprocal Rank Fusion;
- lexical fallback whenever embedding provider or vector index is unavailable.

Vector rollout must be a separate migration/PR because it changes the shared PostgreSQL image and storage schema. It must not weaken tenant DB isolation or send archive text to a new external provider without explicit configuration.
