# voice-picker: плагин выбора голоса ElevenLabs для Hermes

Плагин для Hermes Agent 0.21 (Vektor-сборка), позволяющий владельцу профиля и техническим администраторам интерактивно переключать голос ассистента ElevenLabs в Telegram без рестарта бота.

## Что делает

1. По команде `/golos` (или `/voice_pick`) в личном чате Telegram отправляет reply-клавиатуру (2 колонки × 5 рядов) со списком из 10 голосов ElevenLabs (`eleven_v4_turbo`). При недоступности PTB-бота возвращает пронумерованный список.
2. Сохраняет состояние ожидания выбора для `user_id` в памяти и в `~/.hermes/voice-picker/state.json` (права 0600, TTL 10 минут). Повторный `/golos` сбрасывает таймер, при истечении таймаута ожидание молча сбрасывается.
3. Через hook `pre_gateway_dispatch` перехватывает выбор (нажатие кнопки или ввод цифры 1–10) **до передачи сообщения в LLM**:
   - Атомарно перезаписывает `tts.elevenlabs.voice_id` и `tts.elevenlabs.model_id: eleven_v4_turbo` в `config.yaml` с сохранением остальных ключей и прав 0600.
   - Скрывает reply-клавиатуру (`ReplyKeyboardRemove`).
   - Отвечает подтверждением («Голос: Марина. Чтобы услышать — напиши /voice и задай любой вопрос. Сменить — /golos»).
   - Любой другой текст не перехватывается и передаётся в LLM как обычно.
4. Безопасность: принимает команды и выбор только от пользователей, чьи ID указаны в `platforms.telegram.extra.business_owner_ids` и `allow_admin_from` в `config.yaml`. Чужие сообщения игнорируются. Никакие секреты не читаются. Логируется только «voice changed» без идентификаторов пользователей.

## Голоса

- ♂ Артём — подкаст (`rQOBu7YxCDxGiFdTm28w`)
- ♂ Денис — рассказчик (`0BcDz9UPwL3MpsnTeUlO`)
- ♂ Дмитрий — деловой (`hU3rD0Yk7DoiYULTX1pD`)
- ♂ Николай — YouTube (`3EuKHIEZbSzrHGNmdYsx`)
- ♂ Станислав — спокойный (`ogi2DyUAKJb7CEdqqvlU`)
- ♀ Марина — тёплый рассказчик (`ymDCYd8puC7gYjxIamPt`)
- ♀ Надя — энергичная (`gedzfqL7OGdPbwm0ynTP`)
- ♀ Лариса — молодая, соцсети (`AB9XsbSA4eLG12t2myjN`)
- ♀ Sarah — уверенная (EN) (`EXAVITQu4vr4xnSDxMaL`)
- ♀ Lily — бархатная (EN) (`pFZP5JQG7iQjIQuC4Bku`)

## Установка

Запуск установщика с указанием директории профиля Hermes:

```bash
/opt/vektor/releases/hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2/venv/bin/python install.py --hermes-home ~/.hermes
```

Установщик:
- Копирует плагин в `<home>/plugins/voice-picker` (права 0700/0600, владелец home).
- Создаёт резервную копию `config.yaml` в `<home>/backups/voice-picker-install-<ns>/`.
- Добавляет `voice-picker` в `plugins.enabled` в `config.yaml` (идемпотентно).

После первой установки требуется перезапуск сервиса Hermes (`restart_required=true`).

## Проверка

1. **Тесты:**
   ```bash
   PYTHONPATH=/opt/vektor/vendor/i-hate-editing-audit/.venv/lib/python3.11/site-packages \
   /opt/vektor/releases/hermes-0.21.0-29112bef-vektor4-modern-tglf1-vtm1-tgrel2-pmfix2-pi2-li2-groupss3-lm2/venv/bin/python -m pytest -q tests
   ```

2. **В Telegram:**
   - Открыть личный чат с ботом с аккаунта владельца/администратора.
   - Отправить команду `/golos` или `/voice_pick`.
   - В появившейся клавиатуре выбрать голос (например, «♀ Марина — тёплый рассказчик») или отправить цифру 6.
   - Убедиться, что клавиатура скрылась и бот ответил подтверждением.
   - Проверить обновление `tts.elevenlabs.voice_id` в `~/.hermes/config.yaml`.
