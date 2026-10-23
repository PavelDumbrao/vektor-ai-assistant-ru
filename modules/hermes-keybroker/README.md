# `proai-keybroker` — локальный брокер API-ключей для клиентских ботов Hermes

`proai-keybroker` — высокопроизводительный асинхронный локальный reverse-proxy сервис на базе `aiohttp` и стандартной библиотеки Python, предназначенный для централизованного хранения API-ключей и разграничения доступа между независимыми ботами клиентов на сервере Hermes.

---

## 1. Архитектура и принцип работы

Каждый клиентский бот работает под отдельным Linux-пользователем (например, `bebov` с UID `1009`). Чтобы клиенты не хранили общие API-ключи платформы в своих локальных конфигурационных файлах:

1. **Изолированные полосы (Lanes):**
   - Брокер слушает `127.0.0.1:<порт полосы>` для каждого клиента (например, порт `21001` для клиента `bebov`).
   - Бот клиента отправляет запросы на порт своей полосы без указания API-ключей (например, `http://127.0.0.1:21001/lingsuan/v1/chat/completions`).
   - Фаервол сервера (iptables/nftables) ограничивает доступ к порту полосы только для UID соответствующего Linux-пользователя.

2. **Подстановка секретов и очистка заголовков:**
   - Брокер находит наиболее подходящий маршрут (longest prefix match).
   - Удаляются все hop-by-hop заголовки (`Connection`, `Keep-Alive`, `Transfer-Encoding`, `Upgrade`, `Proxy-*`).
   - Удаляются заголовки `host`, `content-length`, все клиентские auth-заголовки (`Authorization`, `xi-api-key`, `x-api-key`, `api-key`) **и заголовок авторизации маршрута** `auth.header` (регистронезависимо).
   - Удаляются `cookie`, `forwarded`, `x-forwarded-*`, `x-real-ip`, `via`.
   - Принудительно устанавливается собственный заголовок `Accept-Encoding: identity`.
   - В исходящем ответе клиенту удаляются hop-by-hop заголовки и `set-cookie`.
   - Подставляется единственный настоящий API-ключ из переменных окружения процесса брокера (`RouteAuth`).
   - Секретные ключи **никогда не пишутся в логи, тело ответов или базу данных**.

3. **Потоковая передача (Streaming) и отказоустойчивость учёта:**
   - Запросы и ответы передаются чанками по мере их поступления (`web.StreamResponse` с `aiohttp.ClientSession(auto_decompress=False)`).
   - SSE (`text/event-stream`) стримится в реальном времени клиенту без накопления в буфере.
   - Чтение и запись обёрнуты в гарантированный блок `try/finally`: при любом обрыве соединения (клиентом или сервером) строка учёта гарантированно сохраняется с зафиксированными байтами и кодом ошибки (`client_disconnected` или `upstream_disconnected`).

4. **Ограничения (Limits) и конкурентность:**
   - `rpm`: скользящее окно 60 секунд по полосе с учётом запросов в полёте (`in-flight`). Ошибки валидации (`bad_path`, `lane_limit`, `path_not_allowed`, `unknown_route`) не расходуют `rpm`.
   - `concurrency`: семафор параллельных upstream-запросов на полосу (по умолчанию 16). Превышение сразу возвращает `429 lane_limit` с `limit: concurrency` без зависания в очереди.
   - `elevenlabs_chars_month`: лимит символов синтеза речи в текущем календарном месяце (UTC). Для ElevenLabs POST-запросов длина текста резервируется в in-flight до обращения к upstream.
   - Медленный читатель отсекается по таймауту 60 с на чанк (`asyncio.wait_for(resp.write(chunk), 60)`).

5. **Отказоустойчивый STT (Speech-to-Text):**
   - Для маршрутов с `kind: stt` брокер принимает `multipart/form-data` от стандартных клиентов OpenAI SDK, преобразует аудио в base64 и формирует JSON-запрос к OpenRouter.
   - Поддерживается лестница моделей (`ladder`): при 429, 5xx, таймауте, ошибке сети или пустом тексте автоматически выполняется повтор со следующей моделью из списка.

---

## 2. Конфигурация

### Переменные окружения процесса

| Переменная | По умолчанию | Описание |
|---|---|---|
| `KEYBROKER_LANES` | `/etc/proai-keybroker/lanes.json` | Путь к файлу конфигурации полос клиентов |
| `KEYBROKER_ROUTES` | `/etc/proai-keybroker/routes.json` | Путь к файлу конфигурации маршрутов |
| `KEYBROKER_BIND` | `127.0.0.1` | IP-адрес привязки сокетов |
| `KEYBROKER_ADMIN_PORT` | `21000` | Порт административного сайта (healthcheck, статистика) |
| `KEYBROKER_STATE_DIR` | `/var/lib/proai-keybroker` | Каталог состояния (SQLite БД `usage.db`) |
| `KEYBROKER_LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

### Файл полос (`lanes.json`)

Определяет список клиентов, их UID, индивидуальные порты и лимиты.

Пример (`deploy/lanes.example.json`):
```json
{
  "lanes": [
    {
      "client": "bebov",
      "uid": 1009,
      "port": 21001,
      "limits": {
        "elevenlabs_chars_month": 60000,
        "rpm": 90
      }
    },
    {
      "client": "alice",
      "uid": 1010,
      "port": 21002,
      "limits": {
        "rpm": 60
      }
    }
  ]
}
```

* `client`: имя Linux-пользователя (используется для учёта).
* `uid`: числовой идентификатор пользователя (для проверок и отчётности).
* `port`: уникальный порт полосы (диапазон `1024..65535`).
* `limits` (необязательно):
  * `rpm`: максимум запросов за 60 секунд.
  * `elevenlabs_chars_month`: максимум символов ElevenLabs за текущий месяц (UTC).

---

### Файл маршрутов (`routes.json`)

Определяет правила маршрутизации префиксов на upstream-провайдеров.

Пример (`deploy/routes.example.json`):
```json
{
  "routes": [
    {
      "prefix": "/lingsuan/v1",
      "upstream": "https://lingsuan.top/v1",
      "kind": "openai",
      "auth": {
        "header": "Authorization",
        "format": "Bearer {key}",
        "key_env": "LLM_API_KEY"
      }
    },
    {
      "prefix": "/stt/v1",
      "upstream": "https://openrouter.ai/api/v1",
      "kind": "stt",
      "auth": {
        "header": "Authorization",
        "format": "Bearer {key}",
        "key_env": "OPENROUTER_API_KEY"
      },
      "ladder": [
        "openai/whisper-large-v3-turbo",
        "microsoft/mai-transcribe-2",
        "openai/whisper-large-v3"
      ]
    },
    {
      "prefix": "/elevenlabs",
      "upstream": "https://api.elevenlabs.io",
      "kind": "elevenlabs",
      "auth": {
        "header": "xi-api-key",
        "format": "{key}",
        "key_env": "ELEVENLABS_API_KEY"
      },
      "allow": [
        {"methods": ["POST"], "path": "^/v1/text-to-speech/[A-Za-z0-9]+(/stream|/with-timestamps|/stream/with-timestamps)?$"},
        {"methods": ["GET"], "path": "^/v1/voices(/[A-Za-z0-9]+)?$"},
        {"methods": ["GET"], "path": "^/v1/models$"}
      ]
    }
  ]
}
```

Параметры маршрута:
* `prefix`: префикс входящего URL пути (например, `/elevenlabs`, `/lingsuan/v1`). При совпадении всегда выбирается самый длинный префикс (longest prefix match) независимо от порядка маршрутов в файле.
* `upstream`: базовый URL провайдера (строго `https://`, протокол `http://` допустим исключительно для `127.0.0.1`/`localhost`).
* `kind`: тип маршрута (`openai`, `stt`, `elevenlabs`, `generic`, `mcp`).
* `auth`:
  * `header`: имя авторизационного заголовка (`Authorization`, `xi-api-key`).
  * `format`: шаблон значения (например, `Bearer {key}` или `{key}`).
  * `key_env`: имя переменной окружения, откуда берётся секрет.
* `allow` (обязательно): правила доступа для маршрута:
  * Если маршрут открыт целиком, задаётся `"allow": "*"` (при старте логируется WARNING).
  * Иначе задаётся список правил: `[{"methods": ["GET", "POST"], "path": "^/v1/..."}]`.
  * `methods`: список разрешённых HTTP-методов (если опущен — разрешены любые методы).
  * `path`: регулярное выражение (сопоставляется через `fullmatch` с `subpath` после `prefix`).
  * Неизвестные ключи в правиле (например, опечатка `method` вместо `methods`) вызывают ошибку загрузки конфигурации.
* `timeout_s` (необязательно): таймаут в секундах (по умолчанию 600 для `openai`/`mcp`, 180 для остальных).
* `max_body_mb` (необязательно): макс. размер тела запроса в МБ (по умолчанию 64).
* `ladder` (для `kind: stt`): список моделей OpenRouter для отказоустойчивой транскрибации.

---

### Нормализация пути и безопасность (Allowlist & Path Sanitization)

Для предотвращения атак обхода каталогов и несанкционированного доступа:

1. **Строгая валидация raw пути (Path Sanitization):**
   Путь извлекается из `request.raw_path` (до `?`). Немедленно возвращается `400 {"error": "bad_path"}`:
   - если в пути содержится `%` (двойное декодирование и traversal через `%252e%252e` блокируются);
   - если в пути присутствуют символы вне допустимого набора `[A-Za-z0-9._~!$&'()*+,;=:@/-]`;
   - если присутствуют сегменты `.` или `..`;
   - если присутствуют пустые сегменты `//`.
   Query-параметры пробрасываются к upstream **как есть, без перекодирования** (`yarl.URL(target, encoded=True)`). После сборки проверяется, что `url.raw_path` начинается с пути upstream.

2. **Проверка Allowlist путей и методов:**
   После сопоставления маршрута проверяется список `allow`:
   - Если ни одно правило не совпало по методу и регулярному выражению (`re.fullmatch`) — возвращается `403 {"error": "path_not_allowed"}`.

Порядок обработки входящего запроса:
1. Валидация raw пути: `bad_path` (400)
2. `Upgrade: websocket` (501)
3. Проверка `rpm` полосы с учётом in-flight (429 `lane_limit`)
4. Поиск маршрута по longest-prefix (404 `unknown_route`)
5. Проверка `allow` (403 `path_not_allowed`)
6. Проверка наличия API-ключа в env (503 `route_unavailable`)
7. Проверка лимитов полосы (`concurrency`, `elevenlabs_chars_month` с in-flight резервированием) (429 `lane_limit`)
8. Отправка запроса в upstream (потоковый прокси или STT лестница)

---

## 3. Схема базы данных учёта (`usage.db`)

База данных SQLite располагается по пути `$KEYBROKER_STATE_DIR/usage.db` и работает в режиме `WAL` (`Write-Ahead Logging`).

Запись в БД выполняется через внутреннюю очередь `asyncio.Queue` и фоновый воркер: входящие запросы не блокируются операциями дискового ввода-вывода.

### Структура таблицы `requests`

```sql
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,                 -- Unix timestamp (UTC)
    client TEXT,             -- Имя клиента (Linux user)
    port INT,                -- Порт входящей полосы
    route TEXT,               -- Префикс совпавшего маршрута (например, /elevenlabs)
    kind TEXT,                -- Тип маршрута (openai, elevenlabs, stt...)
    method TEXT,              -- HTTP-метод (GET, POST...)
    path TEXT,                -- Путь без query-параметров
    status INT,               -- HTTP статус-код ответа
    req_bytes INT,            -- Размер входящего тела запроса в байтах
    resp_bytes INT,           -- Размер отправленного клиенту ответа в байтах
    duration_ms INT,          -- Время обработки запроса в миллисекундах
    model TEXT,               -- Имя модели (из ответа OpenAI или выбранная в STT)
    prompt_tokens INT,        -- Входные токены (OpenAI)
    completion_tokens INT,    -- Выходные токены (OpenAI)
    chars INT,                -- Символы синтеза (ElevenLabs, заголовок x-character-count)
    error TEXT                -- Короткий код ошибки (lane_limit, upstream_unreachable...)
);

CREATE INDEX IF NOT EXISTS idx_requests_client_ts ON requests(client, ts);
CREATE INDEX IF NOT EXISTS idx_requests_kind_ts ON requests(kind, ts);
CREATE INDEX IF NOT EXISTS idx_requests_port_ts ON requests(port, ts);
```

Срок хранения данных — 13 месяцев. Фоновая задача раз в сутки автоматически выполняет очистку устаревших записей (`purge_older_than(395)`).

---

## 4. Запуск и управление

### Ручной запуск

```bash
KEYBROKER_LANES=/opt/vektor-dev/keybroker/deploy/lanes.example.json \
KEYBROKER_ROUTES=/opt/vektor-dev/keybroker/deploy/routes.example.json \
KEYBROKER_STATE_DIR=/opt/vektor-dev/keybroker \
/opt/vektor-dev/keybroker/.venv/bin/python -m keybroker
```

### Запуск через systemd

1. Развёртывание и установка юнита производятся через скрипт деплоя (единственный источник конфигурации сервиса):
   ```bash
   bash deploy/deploy-keybroker.sh
   ```
2. Перезагрузка конфигурации полос без простоя (Zero-Downtime Reload):
   ```bash
   systemctl reload proai-keybroker
   # или напрямую через сигнал SIGHUP:
   kill -HUP $(pgrep -f "python -m keybroker")
   ```
   **Важно:** Маршруты (`routes.json`) при reload (SIGHUP) **не перечитываются** — перезагружаются только полосы клиентов (`lanes.json`). Для обновления конфигурации маршрутов требуется перезапуск брокера (`systemctl restart proai-keybroker`).
   
   Особенности перезагрузки полос:
   - Лимиты (`rpm`, `elevenlabs_chars_month`, `concurrency`) существующей полосы обновляются на месте без разрыва соединений.
   - При смене `client` или `uid` на порту полоса перезапускается.
   - Ошибка одной полосы (например, порт занят) не прерывает работу остальных; проблемная полоса отмечается со статусом `failed` в `/healthz`.
   - Итог перезагрузки логируется в формате `reload: +N added, -M removed, ~K changed, errors=E`.

---

## 5. Как читать учёт и метрики

### 1. Административный API (порт 21000)

* **Проверка состояния (`GET /healthz`):**
  ```bash
  curl http://127.0.0.1:21000/healthz
  ```
  Ответ:
  ```json
  {
    "ok": true,
    "lanes": [
      {
        "client": "bebov",
        "port": 21001,
        "limits": {
          "elevenlabs_chars_month": 60000,
          "rpm": 90,
          "concurrency": 16
        },
        "in_flight": 0,
        "state": "ok"
      }
    ],
    "routes": 10,
    "queue_depth": 0,
    "dropped_records": 0,
    "last_reload": {
      "ts": 1729670000.0,
      "ok": true,
      "added": 0,
      "removed": 0,
      "changed": 0
    },
    "routes_without_key": []
  }
  ```

* **Агрегированная статистика (`GET /usage`):**
  * По конкретному клиенту за месяц:
    ```bash
    curl "http://127.0.0.1:21000/usage?client=bebov&month=2026-10"
    ```
  * По всем клиентам за текущий месяц:
    ```bash
    curl "http://127.0.0.1:21000/usage"
    ```
  * При некорректном формате месяца (например, `2026-13`) возвращается `400 {"error": "bad_month"}`.
  Ответ:
  ```json
  {
    "ok": true,
    "client": "bebov",
    "month": "2026-10",
    "routes": [
      {
        "route": "/elevenlabs",
        "requests": 42,
        "errors": 0,
        "chars": 12450,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "resp_bytes": 1048576
      },
      {
        "route": "/lingsuan/v1",
        "requests": 150,
        "errors": 1,
        "chars": 0,
        "prompt_tokens": 85000,
        "completion_tokens": 24000,
        "resp_bytes": 5242880
      }
    ]
  }
  ```

### 2. Прямые SQL-запросы к `usage.db`

* **Расход символов ElevenLabs по клиентам за текущий месяц:**
  ```sql
  SELECT client, SUM(chars) AS total_chars, COUNT(*) AS requests
  FROM requests
  WHERE kind = 'elevenlabs'
    AND ts >= strftime('%s', 'now', 'start of month')
  GROUP BY client;
  ```

* **Потребление токенов моделей по клиентам:**
  ```sql
  SELECT client, model, SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens
  FROM requests
  WHERE prompt_tokens IS NOT NULL
  GROUP BY client, model;
  ```

* **Ошибки провайдеров за последние 24 часа:**
  ```sql
  SELECT client, route, status, error, COUNT(*) AS count
  FROM requests
  WHERE status >= 400
    AND ts >= (strftime('%s', 'now') - 86400)
  GROUP BY client, route, status, error;
  ```

### 3. Формат access-лога

Каждый входящий запрос логируется в одну строку (логгер `keybroker.access`) без утечки ключей и персональных данных:
```text
client=bebov port=21001 route=/elevenlabs status=200 ms=124 req=150 resp=4096 chars=120 model= error=
client=alice port=21002 route=/lingsuan/v1 status=429 ms=2 req=0 resp=0 chars=0 model= error=lane_limit
```
Поле `error` содержит строковый код ошибки (например, `lane_limit`, `bad_path`, `client_disconnected`, `payload_too_large`) либо остаётся пустым при успешных ответах.

---

## 6. Запуск тестов

Тесты запускаются строго через виртуальное окружение:
```bash
/opt/vektor-dev/keybroker/.venv/bin/python -m pytest tests -q
```
или с подробным выводом:
```bash
/opt/vektor-dev/keybroker/.venv/bin/python -m pytest tests -v
```
