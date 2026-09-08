# Голос и метаданные документов

Инструменты из работающего серверного профиля, адаптированные к переменным
окружения вместо конкретного имени клиента. Зависимости уже есть в серверном lock:
`edge-tts`, `pypdf`, а также системный `ffmpeg` для Telegram voice.

`bounded_edge_tts.py` принимает пути входного UTF-8 текста и результата MP3.
Один сегмент до 1000 символов, API timeout 35 секунд; настройка Hermes разбивает
более длинный текст по 800 символов. Выход разрешён только в private audio cache
или рабочий каталог указанного профиля. При ошибке ассистент отвечает текстом.

`clean_docmeta.py` расширяет существующий `/usr/local/bin/hermes-clean-docmeta`:
исправляет технического автора свежих PDF, сохраняя версию PDF и метаданные
LibreOffice. Человеческий автор, подписанный/зашифрованный PDF и XMP не меняются.
Обязательные переменные: `HERMES_HOME`, `HERMES_DOC_WORKSPACE`, `HERMES_DOC_AUTHOR`.

Устанавливать в `<HERMES_HOME>/tools/bounded-edge-tts.py` и
`<HERMES_HOME>/hooks/clean-docmeta.py`, owner-only права. Config-owned hook
загружается после планового idle restart; сам файл на диске не доказывает загрузку.
Пример конфигурации находится в `server/profiles/vyacheslav/config.yaml.example`.

Проверка перед передачей: native voice в Telegram, DOCX/PDF вложениями, скачивание
PDF обратно, `pdfinfo`/`pdftotext`, затем один сценарий длинного голоса.
