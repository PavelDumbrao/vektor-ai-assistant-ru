# Серверный Hermes/Vektor

Программный состав действующей установки на 9 сентября 2026 года. Здесь четыре
раздельных профиля на одном VPS: `pavel`, `baysangur`, `vyacheslav`, `bebov`. Они используют
Hermes 0.21.0 (`29112bef099274229cadff79cdff7bf7b99c4b77`) и проверенные Vektor patch-series.
Корневой `hermes-agent/` — прежний учебный снапшот, а не исходники live runtime.

| Профиль | Вариант core | Модель / контекст | Дополнения |
|---|---|---|---|
| pavel | modern | gpt-5.6-sol / 500000 | passive-secretary, video-editor v0.3, Maton, Focus Assistant, AI Fixer; Curator gpt-5.6-terra |
| baysangur | legacy | gpt-5.6-sol / 180000 | passive-secretary, video-editor v0.3, прежний maton-chat-onboarding |
| vyacheslav | modern | gpt-5.6-sol / 180000 | passive-secretary, video-editor v0.3, maton-onboarding, bounded TTS и PDF hook |
| bebov | modern | gpt-5.6-sol / 180000 | passive-secretary с owner-consent для рабочих групп, video-editor v0.3, maton-onboarding, bounded TTS и PDF hook |

У всех FIFO queue, потоковый вывод, один временный статус выполнения. Входящий
архив и исходящие Business-действия имеют раздельные разрешения; исходящие
Business-ответы в этом снимке отключены. Legacy-вариант сохраняет прежнюю политику
существующего профиля и не является шаблоном новых установок. `bebov` переведён на
immutable `vektor4-modern` с trusted technical inviter; финальный consent рабочих групп
остаётся owner-only. Все четыре профиля используют merged Passive Secretary с full-history
recall (Russian FTS + pg_trgm fuzzy fallback); outbound по-прежнему отключён. Общий `video-editor` v0.3 использует root-owned loopback ASR broker с per-profile auth: OpenRouter Whisper Turbo является primary, pinned local whisper.cpp остаётся fallback; captions/cards/hardened public-page proof/SFX/music/look/master/variants доступны без terminal toolset. Изоляция обеспечивается отдельным Linux user, `HERMES_HOME`, процессом и БД.

## Где находятся программы и данные

```text
/opt/vektor/releases/<release-id>/
  hermes-agent/         код, root-owned
  venv/                 pinned Python packages, общие hardlinks
  runtime.json          готовность и hash
/opt/vektor/profiles/<owner>.json
/home/<owner>/.hermes/  личная конфигурация, память, plugins, state.db, cron
/home/<owner>/workspace/
hermes_<owner>          отдельная PostgreSQL БД архива
```

Код и зависимости не изменяются агентами клиентов. Одинаковые файлы физических
версий разделяют hardlinks; процессы, user IDs и данные остаются отдельными.
Это разделение Linux/БД на общем сервере, не четыре отдельные виртуальные машины.

## Что сохранено в GitHub

- `modules/shared-runtime/releases/v0.21.0/vektor3.json`: upstream, patch order,
  hashes двух точных вариантов кода и lock зависимостей.
- `modules/focus-assistant`: 17 native tools, SQLite ledger, брифы, встречи,
  голосовые поручения, почта, проекты, недельный обзор и существующие тесты.
- `modules/ai-fixer-social`: отдельный сервис редактора и профильный Hermes plugin.
- `modules/maton-onboarding` и `modules/maton-legacy`: оба используемых варианта.
- `modules/profile-tools`: ограниченная по времени озвучка и чистые PDF metadata;
  параметры владельца вынесены в env.
- `modules/vektor-live`: отдельный production snapshot голосового Gemini Live bridge
  и Mac Hands; server/web побайтно сверены с `/opt/vektor-live`, release manifest
  фиксирует SHA-256 исходников. Секреты и runtime-state не публикуются.
- `profiles/`: конфигурационные примеры без Telegram ID, подключений и секретов.
- `server/tools/history_backfill.py`: operator-only импорт Telegram Desktop JSON в Passive Secretary с отдельным provenance `history_backfill`, dry-run и идемпотентной записью.
- [RESTORE.md](RESTORE.md): точный порядок сборки/восстановления.

Снимок не включает `.env`, OAuth, живые configuration files, SOUL/USER/MEMORY,
БД, чаты, персональные портреты, вложения, backup archives и реальные connection IDs.
Для восстановления того же аккаунта нужны закрытые резервные копии этих данных.
Перезапись ими новых данных действующего профиля не является обычным деплоем.

## Проверки и выпуск

CI проверяет общие модули, Focus, AI Fixer, profile tools, отсутствие private files
и реконструкцию обоих source trees на Linux. Для обычных modern-client профилей также
проверяется базовый config parity с `vyacheslav`; различия в identity и client-specific
tool surfaces разрешены отдельно. Пофайловый manifest совпадает с VPS.
Существующие тесты не получают настоящие API-ключи и не отправляют сообщения.
`push`/merge не запускает деплой. Живые боты при публикации снимка продолжают работу.
