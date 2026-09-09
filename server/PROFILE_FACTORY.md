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
5. Установить стандартные plugins из GitHub, включая Passive Secretary recall.
6. Создать персональные SOUL/USER только из подтверждённых данных клиента.
7. Подключать Telegram-группы через owner consent; trusted technical inviter может только инициировать enrollment.
8. Старую историю импортировать отдельным `history_backfill`, сохраняя provenance.
9. Прогнать E2E: Telegram DM, group capture, history recall, voice ASR, fallback LLM.
10. Зафиксировать sanitized snapshot/изменение отдельным PR и дождаться CI.

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
