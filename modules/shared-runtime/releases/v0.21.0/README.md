# Hermes 0.21.0 для общего VPS runtime

Официальная основа: [`v2026.8.31`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.31),
commit `29112bef099274229cadff79cdff7bf7b99c4b77`. Версия и SHA-256
исходного tar и патчей закреплены в `release.json`. Лицензия Hermes — MIT.
Это отдельная серверная сборка, а не замена учебного снапшота в корне репозитория.

## Что сохраняют патчи

- Пассивный Telegram inbox, приватные DTO, owner/scope/digest checks и ACK после
  сохранения; Business-сообщения не попадают в обычный диалог с агентом.
- Независимость входящего сбора от корректных дополнительных прав Telegram.
- Одноразовые исходящие подтверждения, исходная команда владельца до любых
  преобразований, запрет автоматического/постоянного согласия и cron-отправки.
- Контекст плагинов `persist: false` только на текущий запрос, без записи в историю.
- Русские команды, управляемый STT fallback и проверка существующего script-файла
  для `no_agent` задач. Новые upstream monitor/continuity поля сохранены.
- Tavily provider из прежнего официального снапшота: в 0.21.0 его удалили,
  но действующие профили продолжают его использовать. Новых зависимостей нет.
- Новые Telegram inline/observer handlers, сетевые pools/keepalive и механизмы
  runtime/approval из 0.21.0 работают вместе с секретарём.

`legacy-capture-policy.patch` — только для сохранения поведения уже существующего
старого профиля, который ещё не переведён на независимую от прав политику.
Не применять его к новым профилям или к профилю с уже исправленным сбором.
Он не меняет private settings, ключи, owner IDs или архив.

## Подготовка администратором

1. Сделайте чистый checkout указанного официального commit в отдельном каталоге.
   Создайте tar через `git archive --format=tar --output=<archive> <commit>`;
   проверьте SHA-256 по `release.json`. GitHub-generated tarball имеет другой
   формат и не заменяет этот артефакт без отдельной проверки.
2. Разместите `prepare_release.py`, `prepare_runtime.py`, `upgrade_profile.py`,
   `switch_profile.py`, `shared_runtime_layout.py` и patch-series в закрытом
   root-owned административном каталоге. Проверьте SHA-256 патчей.
3. `prepare_release.py --archive <tar> --sha256 <verified-sha256>
   --patch <vektor.patch> --release-id <new-release-id>` создаёт только новую
   версию. При необходимости добавьте второй `--patch` для legacy policy.
   `--dependencies <first-release>/requirements.lock` повторно использует
   тот же набор пакетов во второй сборке.
4. Core + MCP зависимости берутся из frozen upstream lock; прочие уже нужные
   профилям пакеты сохраняются из административного lock. PTB закреплён на 22.8.
   Итоговый lock включает hashes; устанавливаются только wheels, через общий uv
   hardlink cache. Venv создаются сразу в окончательных путях. Исходники Hermes
   не выполняются и не собираются от root; `.pth`/metadata создаются отдельно.

## Обязательная проверка до переключения

- Импорты нового runtime и документов от обоих Linux-пользователей.
- Реальная загрузка private plugins на копии HERMES_HOME без `.env`, с запрещённой
  сетью. Ожидаемые плагины должны быть enabled и без ошибок.
- SQLite online backup `state.db`: новая версия открывает копию, количества и
  содержимое messages не меняются, старая версия затем снова открывает ту же
  копию. Нельзя объявлять rollback совместимым только по номеру версии схемы.
- Канонический `scripts/run_tests.sh` на отдельной writable тестовой копии:
  Telegram, turn context, approvals, cron, native plugin compatibility, MCP,
  `tests/test_vektor_021_contracts.py`. Тесты не запускаются от root и не получают
  реальные ключи. Два fake-PTB fixtures расширены для Business API символов.
- В common patch тесты независимости capture рассчитаны на новую политику;
  legacy-вариант проверяется отдельно с ожидаемым старым набором разрешений.
- Сверить final source manifest, root ownership и private permissions. Только
  после успешных проверок администратор фиксирует `state: ready` и буквальное
  `schema_rollback_compatible: true` в `runtime.json` вместе с evidence.

## Переключение

`upgrade_profile.py --owner <linux-user> --release-id <verified-release>` — preview.
Тот же вызов с `--apply` переключает только этот idle-профиль. Есть общий lock,
повторная проверка PID/leases, backup private state, атомарные ссылки и контроль
регистрации. Unit, модели, ключи, SOUL и настройки плагинов не изменяются.

Успех требует новый процесс, `gateway_state=running`, `Telegram=connected`,
живую `code_version=0.21.0` и неизменность защищённых файлов. Затем проверяется
сбор, cron, Maton, документы и изоляция, и только после этого — следующий профиль.

При сбое возвращаются старая ссылка и регистрация; свежие данные не затираются
backup-архивом. При независимом изменении unit/ссылок скрипт требует ручной
проверки. Private backups закрыты root и не публикуются. Старые releases остаются
для отката; не удаляйте их одновременно с обновлением.
