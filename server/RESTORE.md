# Сборка и восстановление текущего VPS

## 1. Проверить исходники

На Linux с case-sensitive файловой системой получите официальный commit из
`vektor3.json` и создайте именно `git archive --format=tar`. GitHub auto-generated
tarball не имеет той же контрольной суммы. Можно использовать сохранённый
`source.tar.gz` из закрытого административного архива: проверяется содержимое tar.

Проверенная копия официального `git archive` также закреплена в
[GitHub Release hermes-vps-2026.09.08](https://github.com/PavelDumbrao/vektor-ai-assistant-ru/releases/tag/hermes-vps-2026.09.08).
CI скачивает её с retries и перед распаковкой проверяет исходный tar checksum.
Это уменьшает зависимость от GitHub upstream rate limits; итоговая проверка
всех файлов modern/legacy остаётся обязательной.

```bash
python3 modules/shared-runtime/verify_release_source.py \
  --archive /absolute/path/source.tar.gz \
  --variant modern --output /absolute/path/new-source-modern
python3 modules/shared-runtime/verify_release_source.py \
  --archive /absolute/path/source.tar.gz \
  --variant legacy --output /absolute/path/new-source-legacy
```

Команды только распаковывают и проверяют код. Они не устанавливают зависимости,
не запускают Hermes и не трогают systemd. Upstream содержит два имени contributor
файлов, отличающихся регистром: обычная macOS volume не даёт точного восстановления.

## 2. Подготовить общий runtime

Используйте существующий административный процесс
`modules/shared-runtime/releases/v0.21.0/README.md`. `prepare_release.py` получает
`vektor.patch`, затем для legacy `legacy-capture-policy.patch`, затем оба
`hermes-021-telegram-*.patch` в порядке из `vektor3.json`. Не патчить уже готовый
release или alias из `/home/<owner>`.

Для точных зависимостей используйте `server-requirements.lock`. Скопируйте его в
новый root-owned каталог внутри `/opt/vektor/releases` и передайте полный путь
через `--dependencies`; digest обязан совпасть с `dependencies_sha256`.
Скрипт ожидает уже подготовленные shared Python/uv из общего runtime runbook.
Новые системные пользователи, PostgreSQL и systemd создаются по основному AGENTS.md.

Unit template: `server/hermes.service.template`, заменить `@OWNER@` и числовой
`@UID@`, установить как `<owner>-hermes.service`. Private TMPDIR создаётся через
tmpfiles по shared-runtime runbook. Текущие systemd limits зафиксированы отдельно
в `server/resource-limits.json`; это срез существующего host, не универсальные
лимиты для новой машины. Не переносите `.control` drop-ins вслепую.

## 3. Согласовать plugin и core

После установки passive-secretary скопируйте его программный normalizer во
временный отдельный каталог и примените `normalizer-modern.patch` либо
`normalizer-legacy.patch` из того же release bundle. Исходник патча —
`modules/passive-secretary/passive_secretary_plugin/normalizer.py` текущего repo.
Проверьте `normalizer_sha256` по выбранному варианту, затем замените только этот
программный файл на остановленном idle-профиле. Остальные plugin `.py` в снимке
совпадают с общим модулем. Settings, owner allowlist и state не менять.

Focus: install.py для новой установки; profile_seven.py только для существующего
Focus и ожидаемых owner job IDs/markers/hashes. Старые персональные cron names
сохранены в migration script; не выполнять его для произвольного клиента.
AI Fixer устанавливается только в профиль редактора по своему README.
Maton legacy сохраняет собственный operator module и приватные settings;
новому клиенту используется современный installer.

## 4. Восстановить профиль

Примеры в `server/profiles` не являются копиями живых `.env/config` и требуют
заполнения владельцем: Telegram ID, bot token, provider key и личные подключения.
Для существующего профиля восстановите private files из отдельного backup,
сохранив owner-only права. Не копировать память и OAuth соседнего пользователя.
Регистрация `/opt/vektor/profiles/<owner>.json` связывает owner с verified release.

Обновление работающего профиля выполняется штатным `upgrade_profile.py`:
preview → idle/backup/source-drift check → apply → running/Telegram ready. Только
после проверки одного профиля переходить к следующему. Эта публикация GitHub
не требует повторного переключения уже совпадающего VPS.

## 5. Приёмка

Проверить getMe/owner binding, реальный ответ, web search, DOCX/PDF после
скачивания из Telegram, входящий/исходящий голос, одноразовое напоминание,
Maton status без секретов и реальное новое Business-сообщение в архиве.
Проверить отсутствие доступа к соседним private homes/DB. Старые code releases
и private backups не удалять одновременно с новым выпуском.

## ВЕКТОР Live и Mac Hands

Исходники production snapshot находятся в `modules/vektor-live`. Перед восстановлением
запустите `python3 modules/vektor-live/verify_release.py`: все 20 production-файлов
должны совпасть с release manifest.

VPS: скопируйте `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `app/` и `web/`
в `/opt/vektor-live`, восстановите `.env` из закрытого хранилища и выполните
`docker compose up -d --build`. Не создавайте ключи из GitHub: `GEMINI_API_KEY`,
`HERMES_API_KEY`, `VEKTOR_ADMIN_KEY` и `VEKTOR_HANDS_KEY` являются private state.
Проверка после запуска: контейнер `vektor-live` работает, `/health` возвращает `ok: true`
и Hermes доступен.

Mac Hands: перенесите `modules/vektor-live/hands/`, установите зависимости из
`hands/requirements.txt`, восстановите `VEKTOR_HANDS_KEY`/`VEKTOR_HANDS_URL` в
`~/.claude/secrets/vektor-live.env` и запустите `python3 hands/vektor_hands.py`.
macOS должен выдать процессу Accessibility и Screen Recording. В логах VPS должно
появиться подключение рук. Host-specific launcher и SSH routing намеренно не лежат
в публичном репозитории.
