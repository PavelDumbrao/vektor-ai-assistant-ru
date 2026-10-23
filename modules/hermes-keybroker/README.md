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
   - Удаляются любые входящие авторизационные заголовки (`Authorization`, `xi-api-key`, `x-api-key`, `api-key`).
   - Подставляется единственный настоящий API-ключ из переменных окружения процесса брокера (`RouteAuth`).
   - Секретные ключи **никогда не пишутся в логи, тело ответов или базу данных**.

3. **Потоковая передача (Streaming):**
   - Запросы и ответы передаются чанками по мере их поступления (`web.StreamResponse` с `aiohttp.ClientSession(auto_decompress=False)`).
   - SSE (`text/event-stream`) стримится в реальном времени клиенту без накопления в буфере.

4. **Ограничения (Limits):**
   - `rpm`: скользящее окно 60 секунд по полосе.
   - `elevenlabs_chars_month`: лимит символов синтеза речи в текущем календарном месяце (UTC) по маршрутам вида `elevenlabs`.
   - При превышении отдаётся `429 Too Many Requests`, а upstream-провайдер не вызывается.

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
* `upstream`: базовый URL провайдера.
* `kind`: тип маршрута (`openai`, `stt`, `elevenlabs`, `generic`, `mcp`).
* `auth`:
  * `header`: имя авторизационного заголовка (`Authorization`, `xi-api-key`).
  * `format`: шаблон значения (например, `Bearer {key}` или `{key}`).
  * `key_env`: имя переменной окружения, откуда берётся секрет.
* `allow` (необязательно): список разрешённых правил доступа:
  * `methods`: список разрешённых HTTP-методов в верхнем регистре (например, `["POST", "GET"]`). Если поле опущено — разрешён любой метод.
  * `path`: регулярное выражение (проверяется по `subpath` после `prefix`; если запрос сделан на сам `prefix`, `subpath` трактуется как `"/"`).
  * Если `allow` опущен — маршрут остаётся открытым, а при старте выводится предупреждение `WARNING: Route %s has no allow list`.
* `timeout_s` (необязательно): таймаут в секундах (по умолчанию 600 для `openai`/`mcp`, 180 для остальных).
* `max_body_mb` (необязательно): макс. размер тела запроса в МБ (по умолчанию 64).
* `ladder` (для `kind: stt`): список моделей OpenRouter для отказоустойчивой транскрибации.

---

### Нормализация пути и безопасность (Allowlist & Path Sanitization)

Для предотвращения несанкционированного доступа (например, вызова функций управления голосами или балансом аккаунта) и атак обхода каталогов:

1. **Нормализация пути (Path Sanitization):**
   До сопоставления маршрута запрос проверяется на недопустимые последовательности:
   - сегменты `..` и `.`;
   - пустые сегменты (`//`);
   - символы `\` (обратный слеш) и `%00` / `\x00` (null-byte).
   При обнаружении немедленно возвращается `400 {"error": "bad_path"}`, обращение к upstream блокируется, а в учёт записывается `status=400, error="bad_path"`.

2. **Проверка Allowlist путей и методов:**
   После сопоставления маршрута проверяется список `allow`:
   - Если ни одно правило не совпало по методу и регулярному выражению пути — немедленно возвращается `403 {"error": "path_not_allowed"}`.
   - Проверка `allow` выполняется **до проверки лимитов** (`rpm` и `chars`) и **до обращения к upstream**, исключая расход лимитов при запрещённых запросах.

Порядок обработки входящего запроса:
1. `bad_path` (400)
2. `Upgrade: websocket` (501)
3. Поиск маршрута по longest-prefix (404 `unknown_route`)
4. Проверка `allow` (403 `path_not_allowed`)
5. Проверка наличия API-ключа в env (503 `route_unavailable`)
6. Проверка лимитов полосы (`rpm`, `elevenlabs_chars_month`) (429 `lane_limit`)
7. Отправка запроса в upstream (потоковый прокси или STT лестница)

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

1. Скопировать юнит в systemd:
   ```bash
   cp deploy/proai-keybroker.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now proai-keybroker
   ```
2. Перезагрузка конфигурации полос без простоя (Zero-Downtime Reload):
   ```bash
   systemctl reload proai-keybroker
   # или напрямую через сигнал SIGHUP:
   kill -HUP $(pgrep -f "python -m keybroker")
   ```
   При получении `SIGHUP` брокер перечитывает `lanes.json`: новые полосы поднимаются, удалённые останавливаются, а существующие соединения и активные потоки не прерываются. При синтаксической ошибке в новом файле старые полосы продолжают работать без сбоев.

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
    "lanes": 2,
    "routes": 10,
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
