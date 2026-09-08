# Telegram toolkit: редактор, а не полный доступ к аккаунту

Этот набор действует только в @ProAiCommunity и в текущей личке Павла для
предпросмотров. Ответы на разрешённые комментарии остаются через прежний `reply`.
Общий send_message, другие чаты, личные рассылки, баны, удаления, выдача прав,
приглашения, Stars, stories и новый cron этим набором не включены.

## Общий порядок

1. `ai_fixer(op="tg_capabilities")` показывает текущие права и поддержанные форматы.
2. Сохрани UTF-8 JSON в `/home/pavel/workspace/ai-fixer-media`. Ни chat_id, ни
   токенов, ни названий методов Telegram API в нём быть не должно.
3. `ai_fixer(op="tg_validate", request_id="vektor-tg-YYYYMMDD-topic-v1",
   telegram_spec_path="/home/pavel/workspace/ai-fixer-media/topic-v1.json")`.
   Проверка без отправки возвращает точный payload_hash.
4. Для нового материала: `tg_preview` с теми же request_id/telegram_spec_path.
   Он сам проверит JSON и отправит нужный нативный формат только Павлу от ВЕКТОРА.
   Это не публичная публикация. После успеха не дублируй пост/файлы/JSON/MEDIA.
   Несколько предпросмотров отправляй последовательно, не параллельными вызовами:
   дождись завершения предыдущего перед следующим.
5. Только после отдельного одобрения: `tg_publish` с тем же файлом и
   `confirm_hash=payload_hash`. Hermes дополнительно запросит одноразовое подтверждение.
6. Для правки существующего сообщения: `tg_objects` -> JSON действия ->
   `tg_validate` -> покажи Павлу цель и изменение -> `tg_manage` с confirm_hash
   после его поручения. Сначала выбери message_id из реестра, не выдумывай его.

`tg_preview` создаёт только новые приватные образцы, а не правит публичный пост.
Для публичных изменений работают только tg_manage и одноразовое owner approval.
Создание публикаций сохраняет общий интервал 6 часов. Не обходить его вторым ботом.

## Опросы и викторины

Настоящий Telegram poll, а не текстовый список вариантов:

```json
{"kind":"poll","question":"Что первым поручим ИИ?","options":["Идеи","Черновики","Превью"],"allows_multiple_answers":true,"open_period":86400}
```

```json
{"kind":"quiz","question":"Когда ВЕКТОР публикует одобряемый черновик?","options":["После подтверждения Павла","Сразу после генерации","После любого лайка"],"correct_option_ids":[0],"explanation":"Приватный показ не заменяет разрешение на публикацию."}
```

- Вопрос 1–300 знаков, 1–12 различных вариантов по 1–100 знаков. Вопрос/варианты
  обычный текст, без HTML. Опросы только анонимные, без сбора индивидуальных голосов.
- `correct_option_ids` это массив индексов с нуля, отсортированный, без повторов.
  Если правильных ответов несколько, нужен allows_multiple_answers=true.
- Explanation у quiz до 200 знаков и двух переносов.
- Необязательно: shuffle_options, hide_results_until_closes (boolean).
- `open_period`: 5–2628000 секунд; вместо него можно close_date (Unix timestamp
  в том же будущем диапазоне). Не задавай оба одновременно.
- Вопрос, варианты и правильный ответ после публикации не переписываются:
  если они ошибочны, предложи закрыть опрос и подготовить новый с одобрением.
- Результаты публичных опросов: `tg_objects(message_id=...)`. Это агрегаты из
  обновлений существующего poller, не живой запрос getPoll (такого метода нет).
  Показывай updated_at и не выдавай старые/нулевые данные за свежую проверку.
  Индивидуальные голоса, а также результаты приватных тестов этим реестром не читаются.

## Текст и кнопки-ссылки

```json
{"kind":"text","text":"<b>Заголовок</b>\n\nКороткий полезный текст.","buttons":[[{"text":"Открыть канал","url":"https://t.me/ProAiCommunity"}]],"link_preview":false}
```

Текст до 4096 знаков по Telegram-ограничению; разметка как в обычных постах.
buttons это строки кнопок, только публичные https-ссылки. До 4 строк, до 4 кнопок
в строке, до 12 всего. Никаких callback_data, оплат, web_app или скрытых действий.
Кнопки поддержаны на тексте, одиночном медиа и опросе, не на фотоальбоме.

## Фото, видео, звук и файлы

```json
{"kind":"photo","file_path":"/home/pavel/workspace/ai-fixer-media/preview.png","caption":"<b>Подпись под фото</b>","buttons":[[{"text":"Канал","url":"https://t.me/ProAiCommunity"}]]}
```

Меняй kind и указывай существующий проверенный файл:

- photo: PNG/JPEG/WebP, до 9 MiB;
- video: MP4; animation: GIF/MP4;
- audio: MP3/M4A, можно title и performer;
- voice: OGG/Opus;
- document: PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/JSON/ZIP, не исполняемые файлы/секреты.

Все вложения одного запроса вместе до 50000000 bytes. Caption до 1024 знаков,
HTML разрешён. Не режь текст молча: для длинного материала используй long_preview.
Для photo/video/animation можно has_spoiler=true. Для всех новых форматов
можно silent=true и protect_content=true. Исходные URL и base64 не принимаются,
только file_path внутри ai-fixer-media. Секретные файлы никогда не прикладывать.

Фото/видеоальбом (не статья с пролистываемой каруселью):

```json
{"kind":"album","items":[{"kind":"photo","file_path":"/home/pavel/workspace/ai-fixer-media/first.png","caption":"Общая подпись"},{"kind":"photo","file_path":"/home/pavel/workspace/ai-fixer-media/second.png"}]}
```

2–10 фото/MP4 в порядке массива. Обычно общий текст только у первого элемента.
Альбом возвращает несколько message_ids: это штатно. Для цельного лонгрида со
slideshow остаётся long_preview, не подменяй его набором отдельных сообщений.

## Управление своими публикациями

Сначала `tg_objects(limit=10)` или `tg_objects(message_id=ID)`. ID в личке
ВЕКТОРА и ID публичного редактора относятся к разным ботам, их нельзя смешивать.
Разрешены только сообщения, опубликованные редактором и занесённые в реестр.

Примеры отдельных JSON-файлов действий:

```json
{"kind":"edit_text","message_id":123,"text":"Исправленный <b>текст</b>"}
```
```json
{"kind":"edit_caption","message_id":123,"caption":"Исправленная подпись"}
```
```json
{"kind":"edit_media","message_id":123,"media_type":"photo","file_path":"/home/pavel/workspace/ai-fixer-media/new.png"}
```
```json
{"kind":"edit_buttons","message_id":123,"buttons":[]}
```
```json
{"kind":"edit_article","message_id":123,"article_path":"/home/pavel/workspace/ai-fixer-media/article-v2.json"}
```
```json
{"kind":"pin","message_id":123}
```
```json
{"kind":"unpin","message_id":123}
```
```json
{"kind":"stop_poll","message_id":123}
```
```json
{"kind":"react","message_id":123,"emoji":"🔥"}
```

Пустой emoji снимает только реакцию бота. Кастомные/платные реакции не включены.
Если не задавать caption при edit_media или buttons при обычной правке,
сохраняются их последние известные значения из реестра. Чтобы убрать кнопки,
передай явный buttons: []. Предыдущее сообщение сохраняется перед изменением.
edit_text работает с обычным текстом. Для RichMessage используй edit_article:
сначала покажи новую статью через long_preview, затем проверь JSON действия через
tg_validate. Для tg_manage нужен именно hash действия (включая message_id), а не
hash отдельной статьи. Старые незарегистрированные публикации не изменяются.

## Ошибки и честность

- Timeout/uncertain или ответ с результатом при ошибке: не повторять ни с тем же,
  ни с новым ID. Сначала независимый read-back.
- Явный Telegram 4xx без результата: rejected, отправка отклонена. Тот же ID не
  запускать заново. Исправленный запрос только после выяснения причины.
- deduplicated означает, что нового действия не было; используй сохранённый ID.
- Не называй валидированный JSON доставленным. Доставка требует message_id и
  независимой проверки. Не обещай полное управление Telegram-аккаунтом.
- Delete/ban/invite/promote/Stories/чужие чаты намеренно недоступны. Не обходи
  это через execute_code, terminal, другой bot token или Telegram user-session.

Документация: https://core.telegram.org/bots/api#sendpoll,
https://core.telegram.org/bots/api#sendmediagroup,
https://core.telegram.org/bots/api#updating-messages.
