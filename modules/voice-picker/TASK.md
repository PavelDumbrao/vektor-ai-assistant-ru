# ТЗ: плагин `voice-picker` для Hermes-профилей Forge (выбор голоса ElevenLabs кнопками)

Рабочая папка: `/opt/vektor-dev/ve-voice-picker` (создать; это НЕ прод). Образец архитектуры и стиля — модуль
`/opt/proai-hermes-manager/vendor/maton-onboarding/` (плагин с `register(ctx)`, `ctx.register_command`,
hook `pre_gateway_dispatch`, `install.py --hermes-home`, тесты). Прочитай его целиком прежде чем писать.
Запрещено: сеть, установка пакетов, git commit/push, systemctl, запись вне рабочей папки, чтение `.env`/секретов,
любые действия в `/home/*` и `/opt/proai-hermes-manager`. Тесты: `python3 -m pytest -q tests` в рабочей папке
(используй venv релиза для импорта Hermes: `/opt/vektor/releases/hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2/venv/bin/python`, читать код можно, менять нельзя).

## Зачем
Клиент выбирает голос ассистента (ElevenLabs v4 Turbo) из 10 вариантов кнопками в Telegram, без рестарта бота.
Hermes читает `tts.elevenlabs.voice_id` из `~/.hermes/config.yaml` при каждом синтезе, поэтому достаточно
атомарно переписать это поле.

## Поведение
1. Команда `/golos` (и алиас `/voice_pick`) в личном чате владельца → бот отправляет сообщение
   «Выбери голос ассистента» с **reply-клавиатурой** (`ReplyKeyboardMarkup`, `one_time_keyboard=True`,
   `resize_keyboard=True`), 2 колонки × 5 рядов, подписи вида «♂ Артём — подкаст», «♀ Марина — тёплый рассказчик».
   Клавиатуру отправлять через PTB-бота адаптера: `adapter.app.bot.send_message(chat_id, text, reply_markup=…)`
   (найди, как адаптер `plugins/platforms/telegram/adapter.py` хранит `app`/`bot`; если недоступно — fallback на
   пронумерованный список и ответ цифрой).
2. Плагин запоминает «ожидаю выбор» для `user_id` (в памяти процесса + файл `~/.hermes/voice-picker/state.json` 0600,
   TTL 10 минут).
3. Hook `pre_gateway_dispatch`: если для этого `user_id` есть ожидание и текст совпадает с подписью кнопки
   (или цифрой 1–10) — перехватить сообщение ДО LLM (как в maton-onboarding), записать `tts.elevenlabs.voice_id`
   и `tts.elevenlabs.model_id: eleven_v4_turbo` в `config.yaml` (прочитать YAML → изменить только эти ключи →
   `yaml.safe_dump(allow_unicode=True, sort_keys=False)` → атомарная запись `mkstemp`+`os.replace`, права 0600,
   владелец не меняется), убрать клавиатуру (`ReplyKeyboardRemove`), ответить «Голос: Марина. Чтобы услышать —
   напиши /voice и задай любой вопрос. Сменить — /golos». Любой другой текст — не перехватывать (вернуть событие как есть).
4. Повторный `/golos` — всегда показывает клавиатуру заново. Таймаут ожидания — молча сбросить.
5. Голоса (константа в плагине, `VOICES = [(label, voice_id, gender)]`, порядок как здесь):
   - ♂ Артём — подкаст: `rQOBu7YxCDxGiFdTm28w`
   - ♂ Денис — рассказчик: `0BcDz9UPwL3MpsnTeUlO`
   - ♂ Дмитрий — деловой: `hU3rD0Yk7DoiYULTX1pD`
   - ♂ Николай — YouTube: `3EuKHIEZbSzrHGNmdYsx`
   - ♂ Станислав — спокойный: `ogi2DyUAKJb7CEdqqvlU`
   - ♀ Марина — тёплый рассказчик: `ymDCYd8puC7gYjxIamPt`
   - ♀ Надя — энергичная: `gedzfqL7OGdPbwm0ynTP`
   - ♀ Лариса — молодая, соцсети: `AB9XsbSA4eLG12t2myjN`
   - ♀ Sarah — уверенная (EN): `EXAVITQu4vr4xnSDxMaL`
   - ♀ Lily — бархатная (EN): `pFZP5JQG7iQjIQuC4Bku`
   Никаких других voice_id (в том числе клонированных) плагин знать не должен.
6. Безопасность: команду и выбор принимать только от владельца профиля и tech-admin — ID брать из
   `platforms.telegram.extra.business_owner_ids` и `allow_admin_from` в config.yaml; чужие сообщения игнорировать.
   Ничего не логировать кроме «voice changed» без ID. Секреты не читать.

## Поставка
- `plugin/__init__.py`, `plugin/plugin.yaml` (name `voice-picker`), `install.py --hermes-home <dir>`:
  копирует плагин в `<home>/plugins/voice-picker` (0700/0600, владелец = владелец home), добавляет
  `voice-picker` в `plugins.enabled` config.yaml (атомарно, бэкап в `<home>/backups/voice-picker-install-<ns>/config.yaml`),
  печатает `plugin=voice-picker` / `telegram_command=/golos` / `restart_required=true`. Идемпотентно.
- `README.md` (кратко: что делает, как ставить, как проверить).
- `tests/`: парсинг выбора (подпись/цифра/мусор), атомарная запись config (другие ключи не меняются,
  права сохраняются), TTL, игнор чужого user_id, идемпотентный install (на временной копии структуры home).

## Отчёт
Что сделано, список файлов, вывод pytest, что не удалось проверить без живого Telegram.
