# Проверка программного снимка

Дата: 8 сентября 2026. Публикация исходников; production-профили не переключались.

- Source reconstruction на отдельном временном Linux-каталоге, без `.env`
  и запуска Hermes: **modern и legacy совпали с VPS**. По **10927** файлов.
- Modern manifest SHA-256:
  `30cf4141ea4e9bdde54babea4c6abd8f108dce6399f1b3cae342a30d46376b49`.
- Legacy manifest SHA-256:
  `91a139bb01dfe907a1999cc9d959465d5e361c2810c4f95215182b28ef3bd8e1`.
- Focus и profile tools: **65 passed**.
- AI Fixer: **185 tests passed**, все внешние действия замоканы.
- Existing release/admin contracts: **59 tests passed**.
- Дополнительный detect-secrets scan выполнен без сетевой проверки значений:
  срабатывания вручную разобраны как SHA-256 manifests/asset guard и синтетические
  значения negative tests. Реальные секреты или приватные профили не публикуются.
- Кандидат публикации сравнен на VPS с 23 действующими credential values:
  совпадений нет. Значения не выводились и не переносились с VPS.
- Живые программы Focus, AI Fixer plugin и social service, legacy Maton
  экспортированы по явному списку Python/YAML-файлов и сверены с исходниками.
- Голос/PDF helpers адаптированы от персональных констант к env; это
  переносимая версия той же логики, а не byte-identical private profile files.
- Патчи normalizer сохраняют два фактически работающих варианта. Современный
  модуль общего репозитория не заменён более старым legacy-кодом.

В этом срезе не проводились новые клиентские Telegram-отправки, live OAuth,
платная генерация, восстановление private DB и rollout на VPS. Они не требуются
для сохранения текущего программного состояния в GitHub.

GitHub CI повторяет тесты и реконструкцию; окончательный результат находится
в проверках pull request и main. Наличие source hash не подтверждает текущее
состояние подключений, архивов или API-балансов — это всегда отдельный live-аудит.
