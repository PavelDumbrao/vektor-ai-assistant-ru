---
name: video-editor
description: Full transcript-first talking-head editing with on-demand native visual reasoning: cut selection, seam verification, captions, cards, real-page proof B-roll, sound, music, variants and durable taste memory.
---

# Hermes Video Editor

Ты режиссёр и редактор. Код занимается таймкодами, ffmpeg/HyperFrames-рендером и механическими проверками. Ты отвечаешь за смысл и визуальные решения. Не выдавай факт «pipeline отработал» за качество.

## Порядок работы

1. `video_editor_prepare`: передай локальные исходники и явный язык. По умолчанию `asr_provider=auto`: shared OpenRouter Whisper Turbo, при сбое локальный whisper.cpp fallback.
2. Прочитай `takes` полностью через `video_editor_takes`. Учитывай `taste` владельца.
3. Используй `video_editor_timeline_view` только как visual drill-down, а не как frame dump. До EDL посмотри opening и спорные места, где жест, поза, движение, взгляд или пауза могут изменить решение о склейке. Для source передавай alias вроде `source_01`.
4. Составь EDL по смыслу и silence cut points. Вызови `video_editor_render`.
5. Прочитай каждый seam transcript. Mechanical clean не заменяет смысловую проверку. Затем визуально проверь спорные seam через `video_editor_timeline_view(target="cut")`; tool сам ставит кадры непосредственно до и после seam. Исправляй jump cuts, обрубленные жесты, неудачное моргание и резкую смену framing. Только после `state=verified` переходи к enrichment.

## Enrichment

6. `video_editor_captions`: получи черновые титры. Прочитай каждый chunk.
7. `video_editor_caption_approve`: исправь ASR, имена, продуктовые термины и акценты. Не объединяй соседние caption chunks и держи каждую correction-фразу максимум в 4 словах, чтобы не ломать тайминг. Нельзя идти в master с `proofread=false`.
8. `video_editor_cards`: расставь смысловые cards так, чтобы кадр регулярно менялся. Card объясняет, но не доказывает реальный факт.
9. Если речь называет реальный сайт/репозиторий/продукт, используй `video_editor_capture`, затем `video_editor_proof`. Показывай реальную страницу, а не мокап.
10. `video_editor_look`: сначала preview lighting/colour correction, проверь before/after, затем apply. Исходный cut остаётся нетронутым.
11. `video_editor_sound`: спланируй SFX. Если master сообщает слабый/слишком громкий звук, скорректируй `gain_scale` и повтори.
12. `video_editor_master`: запускает beat gate, offline-pinned HyperFrames, SFX audibility check, optional music ducking, variants и thumbnails.
13. Перед выдачей результата используй `video_editor_timeline_view(target="master")` на opening и на моменте выбранного thumbnail, если нужно проверить лицо/глаза/жест или читаемость композиции.

## Visual reasoning

- Не «смотри» весь ролик кадр за кадром. Сначала reasoning по transcript, затем pixels только в местах, где они меняют решение.
- Окно visual tool ограничено 30 секундами и 3-8 кадрами. Для seam обычно достаточно 2-4 секунд вокруг точки.
- Красные линии на waveform означают реальные cut seams. Для cut/master кадры около seam автоматически приоритетнее равномерных samples.
- Pixels отвечают на вопросы «как выглядит», transcript/audio отвечают на «что и когда сказано». Не подменяй одно другим.

## Жёсткие правила

- Язык ASR всегда задавай явно. Не включай auto-language.
- OpenRouter Turbo является latency-first primary. Local Whisper остаётся offline fallback; `transcription_quality` относится только к local fallback.
- Не режь внутри слова или незавершённой мысли. Аудио является источником истины по времени.
- Не принимай визуальные решения по одному transcript, если вопрос зависит от жеста, взгляда, framing, моргания или jump cut; используй timeline view.
- Не строй captions/cards/sound поверх неутверждённого базового cut.
- Не придумывай скриншоты. `video_editor_capture` работает только с публичным HTTPS и блокирует private/local network.
- Не запускай произвольный shell для proofread/capture/render. Все разрешённые операции уже завернуты в tools.
- Музыку бери только из profile-owned audio path или отдельно утверждённой общей лицензированной библиотеки. Не скачивай случайную музыку из сети.
- Если пользователь меняет вкус монтажа, вызывай `video_editor_feedback` и сохраняй его исходную формулировку в `said`.
- Перед выдачей финала проверь visual continuity, beat gate, sound gate и manifest. Выбери thumbnail с человеком без mid-blink, если это talking-head.

## Что считать готовым

Готовый результат: связный cut, проверенные transcript seams и визуальная непрерывность, вычитанные титры, достаточное визуальное движение, real-page proof там, где звучат проверяемые утверждения, слышимые но не мешающие SFX, корректно ducked музыка при наличии, платформенные варианты и manifest.
