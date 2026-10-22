# voice-picker: плагин выбора голоса ElevenLabs для Hermes (v2)

Плагин для Hermes Agent 0.21 (Vektor-сборка), позволяющий владельцу профиля и техническим администраторам прослушать примеры и интерактивно переключать голос ассистента ElevenLabs в Telegram без рестарта бота с автоматическим включением голосового режима.

## Что делает (v2)

1. **Примеры голосов до выбора:**
   По команде `/golos` (или `/voice_pick`) в личном чате Telegram бот последовательно отправляет 10 аудиосообщений (`send_voice`) с реальными сэмплами голосов из `/opt/vektor/voice-samples/<voice_id>.ogg` с подписями вида «1. ♂ Артём — подкаст».
   Затем отправляется сообщение «Послушай примеры выше и выбери голос кнопкой 👇» с reply-клавиатурой (2 колонки × 5 рядов). При недоступности PTB-бота или отсутствии сэмплов работает безопасный fallback.

2. **Хранение состояния:**
   Сохраняет состояние ожидания выбора для `user_id` в памяти и в `~/.hermes/voice-picker/state.json` (права `0600`, каталог `0700`, TTL 10 минут). Повторный `/golos` сбрасывает таймер, при истечении таймаута ожидание молча сбрасывается.

3. **Перехват выбора и авто-включение `/voice on`:**
   Через hook `pre_gateway_dispatch` перехватывает выбор (нажатие кнопки или ввод цифры 1–10) **до передачи сообщения в LLM**:
   - Атомарно перезаписывает `tts.elevenlabs.voice_id` и `tts.elevenlabs.model_id: eleven_v4_turbo` в `config.yaml` с сохранением остальных ключей и прав `0600`.
   - Отправляет голосовое подтверждение тем же выбранным голосом (`send_voice`) с подписью:
     `«Готово! Теперь я говорю голосом «<Имя>». Голосовые ответы включены: на голосовое отвечу голосом. Все ответы голосом — /voice tts, выключить — /voice off, сменить голос — /golos»`
     и одновременно скрывает reply-клавиатуру (`ReplyKeyboardRemove`).
   - Возвращает `{"action": "rewrite", "text": "/voice on"}`: шлюз Hermes штатно обрабатывает перезаписанный текст события как команду `/voice on`, включая режим `voice_only` и `auto_tts`.
   - Любой другой текст не перехватывается (`None`) и передаётся в LLM как обычно.

4. **Безопасность:**
   Принимает команды и выбор только от пользователей, чьи ID указаны в `platforms.telegram.extra.business_owner_ids` и `allow_admin_from` в `config.yaml`. Чужие сообщения игнорируются. Никакие секреты не читаются. Логируются только события «voice changed» и «sample send failed» без идентификаторов пользователей.

## Голоса

1. ♂ Артём — подкаст (`rQOBu7YxCDxGiFdTm28w`)
2. ♂ Денис — рассказчик (`0BcDz9UPwL3MpsnTeUlO`)
3. ♂ Дмитрий — деловой (`hU3rD0Yk7DoiYULTX1pD`)
4. ♂ Николай — YouTube (`3EuKHIEZbSzrHGNmdYsx`)
5. ♂ Станислав — спокойный (`ogi2DyUAKJb7CEdqqvlU`)
6. ♀ Марина — тёплый рассказчик (`ymDCYd8puC7gYjxIamPt`)
7. ♀ Надя — энергичная (`gedzfqL7OGdPbwm0ynTP`)
8. ♀ Лариса — молодая, соцсети (`AB9XsbSA4eLG12t2myjN`)
9. ♀ Sarah — уверенная (EN) (`EXAVITQu4vr4xnSDxMaL`)
10. ♀ Lily — бархатная (EN) (`pFZP5JQG7iQjIQuC4Bku`)

## Установка

Запуск установщика с указанием директории профиля Hermes:

```bash
/opt/vektor/releases/hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2/venv/bin/python install.py --hermes-home ~/.hermes
```

Установщик:
- Копирует плагин в `<home>/plugins/voice-picker` (права 0700/0600, владелец home).
- Создаёт резервную копию `config.yaml` в `<home>/backups/voice-picker-install-<ns>/`.
- Добавляет `voice-picker` в `plugins.enabled` в `config.yaml` (идемпотентно).

## Запуск тестов

```bash
PYTHONPATH=/opt/vektor/vendor/i-hate-editing-audit/.venv/lib/python3.11/site-packages \
/opt/vektor/releases/hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2/venv/bin/python -m pytest -v tests
```
