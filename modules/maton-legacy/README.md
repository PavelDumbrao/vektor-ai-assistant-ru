# Существующий Maton onboarding

Сохранён программный вариант `maton-chat-onboarding`, который использует
существующий профиль `baysangur`. Точный plugin находится в `plugin/`.
Его `settings.json`, ключи и pending state являются приватными файлами.

Для новых профилей используйте `modules/maton-onboarding`: это отдельная
реализация с самостоятельным installer. Не заменяйте legacy plugin на неё
автоматически при восстановлении существующего профиля.

Legacy plugin ожидает `settings.json` с owner (Linux user), linux_home,
hermes_home, hermes_python, hermes_agent_dir и activation_module. Пути указывают
на данный профиль; реальные значения восстанавливаются из закрытой конфигурации.
Код activation_module хранится рядом в `operator/`, если он нужен при восстановлении.
Ключ принимается через /maton до передачи сообщения LLM и логирования.
