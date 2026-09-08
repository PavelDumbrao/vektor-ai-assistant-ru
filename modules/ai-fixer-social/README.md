# AI Fixer Social и интеграция Hermes

Программный снимок отдельного редактора Telegram, подключённого к профилю
`pavel`. Личные фото, черновики, публикации, базы, ключи и исторические deploy
скрипты не входят в репозиторий.

- `src/ai_fixer_social/`: сервис комментариев, редактор, проверка контента,
  очередь одобренных публикаций, общий журнал исходящих, GRSAI integration.
- `integrations/hermes/ai_fixer/`: точная программная версия действующего plugin.
- `integrations/hermes/ai-fixer-editor/`: процедурный skill редактора.
- `integrations/telegram_engine/`: read-only analytics bridge.
- `deploy/`: service, privileged broker wrapper, sudoers и env.example.
- `tests/`: существующие unit/negative tests без настоящих внешних отправок.

Это профильная интеграция: launcher явно привязан к Linux user `pavel`, сервису
`ai-fixer-social` и `/opt/ai-fixer-social`. Автоматически подключать его клиентам
нельзя. Для другого профиля нужны отдельный broker, service user и reviewed paths.
Публикация, очередь и генерация требуют существующих однократных подтверждений.

Для восстановления сервиса: создать service user, развернуть код в
`/opt/ai-fixer-social`, установить `pyproject.toml` в отдельное окружение,
заполнить `/etc/ai-fixer-social.env` по `deploy/env.example`, восстановить private
state и проверенный портрет из закрытой резервной копии. Затем установить wrapper
и sudoers после `visudo -cf`, подключить plugin и skill к личному HERMES_HOME.
Режим `COMMENT_MODE=shadow` обязателен до проверки read-back.

Редакционная политика — адаптируемый пример. Месячный контент-план и конкретный
портрет задаёт владелец в private skill references/assets; наличие кода не
заменяет этих входов. Сервисы Telegram API Engine, LLM и GRSAI остаются внешними
зависимостями; их секреты не передаются агенту через аргументы инструмента.

Проверка: `PYTHONPATH=modules/ai-fixer-social/src python -m unittest discover
-s modules/ai-fixer-social/tests -q` из корня репозитория.
