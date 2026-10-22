# ТЗ tgvid1: вертикальные видео в Telegram — размеры, длительность, thumbnail в `send_video`

Рабочая папка: `/opt/vektor-dev/tgvid1/hermes-agent` — копия кода Hermes 0.21 (сборка Вектора) БЕЗ venv. Это НЕ прод.
Запрещено: сеть, установка пакетов, git commit/push, systemctl, запись вне `/opt/vektor-dev/tgvid1`, чтение `/home/*`,
`/opt/vektor/releases/*` менять нельзя (читать можно), `.env`/секреты не читать.
Python и тесты: `/opt/vektor/releases/hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2/venv/bin/python`
(в нём есть pytest? если нет — `PYTHONPATH=/opt/vektor/vendor/i-hate-editing-audit/.venv/lib/python3.11/site-packages` добавь pytest).
Запуск: `cd /opt/vektor-dev/tgvid1/hermes-agent && <python> -m pytest -q -p no:cacheprovider tests/gateway -k telegram` (сначала посмотри, какие тесты
Telegram-адаптера существуют и что они используют как фикстуры/моки).

## Проблема
`plugins/platforms/telegram/adapter.py` → `async def send_video(...)` зовёт `self._bot.send_video` только с `video`, `caption`,
`reply_to_message_id`, thread-kwargs. Без `width`/`height`/`duration` Telegram угадывает размеры → вертикальные ролики (1080×1920)
показываются кривыми/квадратными. Нужно передавать метаданные, не ломая текущее поведение, когда их нельзя получить.

## Что сделать
1. Новый модуль `plugins/platforms/telegram/video_meta.py` (чистые функции, без asyncio):
   - `probe_video(path, *, ffprobe=None, timeout=10.0) -> VideoMeta | None`, где `VideoMeta` — dataclass
     `width: int, height: int, duration: int, rotation: int`. Вызов `ffprobe -v error -select_streams v:0 -show_entries
     stream=width,height,sample_aspect_ratio,codec_type:stream_tags=rotate:stream_side_data=rotation:format=duration -of json <path>`
     (`subprocess.run`, `timeout`, `shutil.which("ffprobe")` если `ffprobe` не задан). Ширина/высота ОТОБРАЖЕНИЯ: учесть
     SAR (`sample_aspect_ratio` вида `a:b`, если не 1:1 — умножить ширину на a/b и округлить до чётного), затем rotation
     (`tags.rotate` или `side_data_list[].rotation`; 90/270/-90/-270 → поменять местами). `duration` — `ceil(format.duration)`,
     минимум 1. Любая ошибка/таймаут/нет ffprobe/нет видеопотока → `None` (никаких исключений наружу), лог `logger.debug`.
   - `make_thumbnail(path, meta, *, ffmpeg=None, timeout=15.0) -> str | None`: `ffmpeg -v error -y -ss <t> -i <path> -frames:v 1
     -vf "scale=<...>" -q:v 5 <tmp.jpg>`, где `t = min(1.0, max(0, meta.duration/2))` при duration ≤ 2, иначе 1.0; `scale` так, чтобы
     длинная сторона ≤ 320 px с сохранением пропорций (учитывая поворот: используй автоповорот ffmpeg, он включён по умолчанию);
     результат — JPEG во временном файле (`tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")`), если размер > 200 КБ —
     перекодировать с `-q:v 10`, если всё равно больше — вернуть `None` и удалить файл. Любая ошибка → `None`.
   - `video_send_kwargs(path) -> tuple[dict, str | None]`: собирает `{"width", "height", "duration", "supports_streaming": True}`
     и путь thumbnail (или пустой dict и `None`, если probe вернул None). Всё это синхронно; в адаптере звать через
     `asyncio.to_thread` (как уже делается для `_probe_audio_metadata`/ffprobe в этом же файле — найди и повтори стиль).
2. В `send_video` адаптера: перед `open(video_path, "rb")` получить `extra, thumb_path = await asyncio.to_thread(video_send_kwargs, video_path)`;
   добавить `extra` в kwargs `self._bot.send_video`; если `thumb_path` — открыть файл и передать `"thumbnail": thumb_file`
   (PTB 22 принимает file-like); `reset_media` при retry должен делать `f.seek(0)` И `thumb_file.seek(0)`; в `finally` закрыть и
   удалить временный thumbnail. Если метаданных нет — вызов ровно как раньше (ни одного нового ключа). Исключения из probe/thumbnail
   никогда не должны ронять отправку.
3. То же для пути, где видео уходит через `send_file`/`send_document`-ветку как `.mp4/.mov/.m4v/.webm`, если такая ветка есть
   и она тоже зовёт `send_video` PTB (найди по `send_video(` в адаптере и в `gateway/platforms/base.py`). `send_animation` не трогать.
4. Тесты (`tests/gateway/test_telegram_video_meta.py` + при необходимости тест адаптера с моком `_bot.send_video`):
   - probe: вертикальное 1080×1920 без rotate → (1080, 1920); горизонтальное 1920×1080 с `tags.rotate=90` → (1080, 1920);
     `side_data rotation=-90` → то же; SAR 4:3 у 1440×1080 → 1920×1080; duration 12.2 → 13; нет ffprobe → None; таймаут → None;
     мусорный JSON → None. ffprobe мокать через параметр `ffprobe` (путь к фейковому скрипту) или `monkeypatch` `subprocess.run`.
   - thumbnail: при недоступном ffmpeg → None; при успешном вызове (мок) → путь существует и удаляется адаптером после отправки.
   - адаптер: с метаданными `send_video` получает `width/height/duration/supports_streaming/thumbnail`; без метаданных — набор ключей
     ровно как раньше (регрессия); retry через `reset_media` перематывает оба файла.
   - Все существующие тесты Telegram-адаптера остаются зелёными.
5. Не менять ничего, кроме `adapter.py` (только `send_video` и, если нужно, импорт), нового `video_meta.py` и тестов.
   В отчёте — точный список изменённых/новых файлов и `diff -u` для `adapter.py` (он будет применён к четырём релизам,
   у которых этот метод побайтно одинаков).

## Отчёт
Файлы, `diff -u adapter.py`, вывод pytest (новые + существующие telegram-тесты), что не проверено (живая отправка в Telegram — её сделаю я).
