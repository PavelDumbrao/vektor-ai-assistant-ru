# Telegram large-file ingress

Безопасный per-profile ingress для Hermes через уже существующий локальный `telegram-bot-api --local`.

## Зачем

Public Bot API ограничивает `getFile` примерно 20 MiB. Local Bot API умеет большие файлы, но возвращает абсолютный server path. Нельзя давать Hermes-пользователям общий доступ к `/opt/telegram-transcriber-bot/data/bot-api`, потому что там лежат каталоги разных bot tokens.

## Архитектура

```text
Telegram
  -> local Bot API 127.0.0.1:8082
  -> token-scoped Bot API directory
  -> per-owner POSIX ACL
  -> root-only neutral alias
  -> systemd read-only bind /run/hermes-telegram-local
  -> disk-backed staging under HERMES_HOME
  -> cache/videos|audio|documents
```

Hermes-профиль не вступает в общую группу `telegram-transcriber`. Shared Bot API root внутри service namespace скрыт через `InaccessiblePaths`.

## Требования

- local Telegram Bot API доступен на `127.0.0.1:8082` и запущен с `--local`;
- host data root: `/opt/telegram-transcriber-bot/data/bot-api`;
- пакет `acl`, команды `setfacl` и `getfacl`;
- существующий `<owner>-hermes.service`;
- Telegram bot credential уже хранится только в private `.env` профиля.

## Установка профиля

Сначала read-only preflight:

```bash
python3 modules/telegram-large-file/install.py --owner pavel --no-restart
```

Затем pilot/apply:

```bash
python3 modules/telegram-large-file/install.py --owner pavel --apply
```

Production default: 1 GiB на файл. Верхняя граница installer: 2 GiB.

## Security contract

- bot token не пишется в GitHub, systemd unit, drop-in или logs;
- drop-in хранит только SHA-256 tenant identifier;
- original token path недоступен Hermes user;
- соседние tenant directories недоступны;
- bind mount read-only;
- path traversal и symlink components отклоняются через dir-fd + `O_NOFOLLOW`;
- incoming video/audio/document не буферизуются целиком в Python RAM;
- staging всегда profile-local: `$HERMES_HOME/cache/telegram-ingress`, mode `0700`;
- final cached file получает mode `0600`.

## Приёмка

Обязательные проверки перед fleet rollout:

1. Profile user не состоит в `telegram-transcriber`.
2. Shared Bot API root не читается ни на host, ни внутри service namespace.
3. Private mount читается и не записывается.
4. Файл >20 MiB попадает в profile cache без `download_as_bytearray()`.
5. `video_editor_prepare` успешно принимает полученный cache path.
6. Сначала pilot одного профиля, затем rollout tenant-by-tenant.
