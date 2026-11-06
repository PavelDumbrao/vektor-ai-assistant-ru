# Модуль автоудаления данных клиента после offboarding (`offboard_purge`)

Короткий runbook и техническое описание модуля автоудаления профилей Hermes Forge.

---

## 1. Контекст и назначение

- **Политика жизненного цикла:** после выполнения команды `/offboard` профиль клиента деактивируется, а его данные сохраняются на сервере в течение **30 дней** с соответствующим предупреждением владельцу.
- **Задача модуля:** по истечении 30-дневного срока (`purge_after <= now`) процесс менеджера (запущенный от `root`) вызывает процедуру полной и безопасной зачистки данных арендатора.
- **Границы ответственности:** модуль `offboard_purge.py` не управляет собственными таймерами и не модифицирует `state.json` менеджера — он предоставляет детерминированный, безопасный и атомарный пайплайн удаления профиля.

---

## 2. Архитектурные гарантии безопасности

1. **Защита от инъекций команд:**
   - Никаких вызовов с `shell=True` или `os.system()`.
   - Все аргументы команд передаются строго списком с экранированием параметров через разделитель `--`.
2. **Строгая валидация владельца:**
   - Имя `<owner>` валидируется регулярным выражением `^[a-z0-9][a-z0-9_-]{1,39}$`.
   - Запрещены системные пользователи (`root`, `bin`, `daemon` и др.).
   - Формирование любых путей к файлам и каталогам выполняется исключительно после успешной валидации имени.
3. **Pre-flight проверка безопасности (`is_safe`):**
   - Пользователь существует в системе (`pwd.getpwnam`).
   - `uid >= 1000` (защита от удаления системных аккаунтов).
   - Домашний каталог находится строго внутри `/home` и не является символической ссылкой.
   - Сервис `<owner>-hermes.service` находится в статусе `inactive`.
   - Отсутствуют запущенные процессы пользователя (`pgrep -u <owner>`).
4. **Гарантия архивации до удаления:**
   - До любых деструктивных действий создаётся полный архив домашнего каталога и дамп PostgreSQL.
   - Если архивация завершилась с ошибкой, удаление немедленно прекращается без изменения состояния системы.
5. **Атомарность конфигурации:**
   - Изменение `/etc/proai-hermes-fleet-policy.json` выполняется атомарно через временный файл с обязательным созданием резервной копии `.bak`.

---

## 3. Последовательность шагов очистки (`purge`)

Каждый шаг выполняется с таймаутом и фиксируется в списке `steps`:

1. **`is_safe`** — проверка предусловий безопасности. При несоответствии — отказ без действий.
2. **`archive`** — финальное резервное копирование:
   - Создание каталога `/var/backups/proai-hermes/offboarded` (права `0700`, владелец `root`).
   - Архивирование дома: `tar --use-compress-program="zstd -q -T2" -C /home -cf <backup_root>/<owner>-<timestamp>.tar.zst <owner>`.
   - Проверка наличия базы в PostgreSQL (`select 1 from pg_database where datname='hermes_<owner>'`).
   - При наличии базы: дамп `pg_dump -Fc hermes_<owner>` в тот же каталог архива.
   - *При ошибке архивации дальнейшие шаги не выполняются.*
3. **`units`** — остановка и удаление юнитов systemd:
   - Отключение юнитов: `systemctl disable --now -- <unit>` для:
     - `<owner>-hermes.service`
     - `<owner>-hermes-passive-secretary-retention.timer`
     - `proai-hermes-shared-metrics-export@<owner>.timer`
     - `proai-hermes-shared-metrics-export@<owner>.service`
   - Удаление файлов юнитов в `/etc/systemd/system/`.
   - Удаление каталогов drop-in конфигураций:
     - `/etc/systemd/system/<owner>-hermes.service.d/`
     - `/etc/systemd/system.control/<owner>-hermes.service.d/`
   - Применение изменений: `systemctl daemon-reload`, сброс состояния: `systemctl reset-failed`.
4. **`lanectl`** — отзыв сетевой полосы брокера ключей:
   - `/usr/local/sbin/lanectl remove --client <owner>` (`check=False`, не блокирует удаление).
5. **`dropdb`** — удаление базы данных PostgreSQL:
   - `docker exec passive-secretary-postgres sh -c '...' sh dropdb --if-exists hermes_<owner>`.
   - Роль PostgreSQL намеренно не удаляется во избежание конфликтов общего кластера.
6. **`userdel`** — удаление учетной записи Linux:
   - `userdel -r -- <owner>` (таймаут 120 с).
   - Дополнительная проверка: если домашний каталог остался на диске, проверяется, что путь строго равен `/home/<owner>` и не является симлинком, после чего удаляется через `shutil.rmtree`.
7. **`cleanup_state`** — очистка реестра и состояния Forge:
   - Удаление файла профиля `/opt/vektor/profiles/<owner>.json`.
   - Атомарное удаление `profiles[<owner>]` из политики `/etc/proai-hermes-fleet-policy.json` (с сохранением `.bak`).
   - Удаление файлов состояния Forge:
     - `/var/lib/proai-hermes-forge/workspace-members/<owner>.json`
     - `/var/lib/proai-hermes-forge/workspace-invites/<owner>.json`
     - `/var/lib/proai-hermes-forge/zoom-bindings/<owner>.json`
     - `/var/lib/proai-hermes-forge/desired/<owner>.json`
8. **Итог:** возврат словаря с метаданными выполнения (`owner`, `ok: True`, `archive`, `steps`, `purged_at`).

---

## 4. Использование API и CLI

### Python API

```python
from offboard_purge import plan, is_safe, purge

# 1. Формирование плана очистки
purge_plan = plan("h503899482")

# 2. Проверка безопасности
safe, reason = is_safe(purge_plan)
if not safe:
    print(f"Safety check failed: {reason}")

# 3. Сухой прогон (без выполнения действий, возвращает план)
dry_result = purge("h503899482", dry_run=True)

# 4. Полное выполнение очистки
result = purge("h503899482")
if result["ok"]:
    print(f"Purge successful. Archive: {result['archive']}")
else:
    print(f"Purge failed at step {result['failed_step']}: {result['steps']}")
```

### CLI

Модуль поддерживает прямой запуск из командной строки:

```bash
# Просмотр плана для арендатора
python3 -m offboard_purge plan h503899482

# Сухой прогон (проверка и вывод плана без изменений на диске)
python3 -m offboard_purge purge h503899482 --dry-run

# Боевое выполнение очистки (только под root)
python3 -m offboard_purge purge h503899482
```

---

## 5. Инструкции для дежурного инженера (Runbook)

### 5.1. Что делать при сбое на шаге `is_safe`
- `unit_active`: основной сервис бота еще запущен. Проверьте статус `systemctl status <owner>-hermes.service`. Очистка возможна только после штатной остановки сервиса.
- `user_processes_running`: в сессии пользователя остались активные фоновые процессы. Проверьте процессы через `pgrep -u <owner> -l` или `ps -u <owner>`.
- `home_is_symlink` или `home_outside_home_root`: подозрительная конфигурация домашней директории. Требуется ручной аудит системного администратора.

### 5.2. Что делать при сбое на шаге `archive`
- Проверьте свободное место в `/var/backups` (`df -h /var/backups`).
- Проверьте доступность контейнера PostgreSQL: `docker ps -f name=passive-secretary-postgres`.
- Никакие данные клиента на этом этапе не удалены. Устраните проблему с диском/БД и повторите запуск.

### 5.3. Восстановление клиента из резервной копии
Если потребовалось восстановить случайно удаленного или передумавшего клиента:
1. Архивы лежат в каталоге `/var/backups/proai-hermes/offboarded/`.
2. Файлы:
   - Домашний каталог: `<backup_root>/<owner>-<timestamp>.tar.zst`
   - Дамп базы: `<backup_root>/hermes_<owner>-<timestamp>.dump`
3. Восстановление домашнего каталога:
   ```bash
   useradd -m -s /bin/bash <owner>
   tar --use-compress-program=zstd -xvf /var/backups/proai-hermes/offboarded/<owner>-*.tar.zst -C /home
   chown -R <owner>:<owner> /home/<owner>
   ```
4. Восстановление базы PostgreSQL:
   ```bash
   docker exec -i passive-secretary-postgres sh -c 'export PGPASSWORD="$POSTGRES_PASSWORD" PGUSER="$POSTGRES_USER" PGHOST=localhost; exec "$@"' sh createdb hermes_<owner>
   docker exec -i passive-secretary-postgres sh -c 'export PGPASSWORD="$POSTGRES_PASSWORD" PGUSER="$POSTGRES_USER" PGHOST=localhost; exec "$@"' sh pg_restore -d hermes_<owner> < /var/backups/proai-hermes/offboarded/hermes_<owner>-*.dump
   ```
5. При необходимости восстановите запись в политике флота из `/etc/proai-hermes-fleet-policy.json.bak`.
