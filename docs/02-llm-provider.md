# «Мозг» ассистента: подписка Claude, подписка ChatGPT или API-ключ

Ассистенту нужна нейросеть (LLM). Платить дважды не придётся: если у вас уже есть подписка Claude или ChatGPT — используем её. Нет ничего — берёте API-ключ (ученикам клуба его выдаёт Павел).

Выберите свой вариант и просто скажите об этом агенту-установщику — команды ниже он выполнит сам.

---

## Вариант 1. Подписка Claude

Hermes умеет входить в аккаунт Claude по OAuth — тем же способом, что Claude Code.

⚠️ **Важное ограничение (из документации Hermes):** через подписку работает только план **Claude Max с докупленными extra usage credits**. План Pro этот путь не поддерживает — если у вас Pro, берите вариант 3 (API-ключ).

Как включает агент:

```bash
~/.hermes/hermes-agent/venv/bin/hermes model   # → Anthropic → пункт «1. Claude Pro/Max subscription (OAuth login)»
```

(Пункт меню называется «Pro/Max», но реально работает только Max с extra credits — это особенность Hermes.)

Откроется страница входа Claude — вы входите в свой аккаунт, дальше всё само. Если на компьютере уже установлен Claude Code, Hermes может подхватить его вход автоматически (macOS Keychain / `~/.claude/.credentials.json`).

Конфиг после этого (агент сделает сам):

```yaml
model:
  provider: "anthropic"
  default: "claude-sonnet-4-6"
```

## Вариант 2. Подписка ChatGPT

Hermes входит в аккаунт ChatGPT напрямую (device-code OAuth). Устанавливать Codex CLI **не нужно**; если Codex CLI уже стоит и вы в нём залогинены — Hermes предложит забрать его вход из `~/.codex/auth.json`.

```bash
~/.hermes/hermes-agent/venv/bin/hermes model   # → OpenAI → openai-codex → вход по коду на странице OpenAI
```

Конфиг:

```yaml
model:
  provider: "openai-codex"
  default: "gpt-5.4"
```

## Вариант 3. API-ключ (самый простой; клубный ключ — сюда)

Подходит любой из трёх источников ключа:

**а) Клубный ключ от Павла.** Пишете Павлу (контакт — в канале [Павел Думбрао | Ai Fixer](https://t.me/ProAiCommunity)), получаете ключ и адрес сервера. Агент прописывает:

```yaml
model:
  provider: custom
  base_url: <адрес, который пришёл вместе с ключом>
  default: <модель, которую назвал Павел>
  api_key: ${LLM_API_KEY}
```

и кладёт ключ в `~/.hermes/.env`: `LLM_API_KEY=...`

**б) Ключ Anthropic** (console.anthropic.com, оплата за токены):

```yaml
model:
  provider: "anthropic"
  default: "claude-sonnet-4-6"
```
`.env`: `ANTHROPIC_API_KEY=sk-ant-...`

**в) Ключ OpenRouter** (openrouter.ai — сотни моделей одним ключом):

```yaml
model:
  provider: openrouter
  default: "anthropic/claude-sonnet-4.6"
```
`.env`: `OPENROUTER_API_KEY=sk-or-...`

---

## Шпаргалка для агента-установщика

- Валидные значения `model.provider` здесь: `anthropic`, `openai-codex`, `openai-api`, `openrouter`, `custom`. Значения **`openai` НЕ существует** (упадёт с `Unknown provider`); прямой API OpenAI = `openai-api`.
- `custom` = любой OpenAI-совместимый endpoint; ключ и `base_url` живут в `config.yaml` (ключ можно через `${ПЕРЕМЕННАЯ}` из `.env`).
- Интерактивный выбор: `~/.hermes/hermes-agent/venv/bin/hermes model` (или `... hermes setup`) — сам проведёт по OAuth-флоу и запишет конфиг. Голый `hermes` не в PATH — всегда полный путь до venv.
- Для подписки Claude токен также принимается через `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_TOKEN` в `.env`.
- Голосовые: расшифровка работает локально из коробки; если есть `OPENROUTER_API_KEY` — можно переключить STT на облако (быстрее на слабых машинах).

---

## Голос ассистента (TTS)

По умолчанию включён **бесплатный** голос (edge): русский мужской `ru-RU-DmitryNeural`; женский — `ru-RU-SvetlanaNeural` (меняется в `config.yaml`, блок `tts.edge.voice`). Хотите премиум-звучание — получите ключ MiniMax, положите `MINIMAX_API_KEY` в `.env` и поставьте `tts.provider: minimax`. Если у платного провайдера кончится баланс, бот сам тихо вернётся на бесплатный.
