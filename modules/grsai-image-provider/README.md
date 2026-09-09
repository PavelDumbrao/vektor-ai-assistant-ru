# GRSAI GPT Image 2.5 provider

Профильный backend генерации изображений для серверных Hermes/Vektor.
Он не меняет общий Hermes core: plugin устанавливается только в
`/home/<owner>/.hermes/plugins/image_gen/grsai` конкретного клиента.

## Контракт

- Provider: `grsai`.
- Model: `gpt-image-2.5`.
- Global API host: `https://grsaiapi.com`.
- Submit: `POST /v1/draw/completions`.
- Poll: `POST /v1/draw/result`.
- Поддерживаются text-to-image и reference image editing через `urls`.
- Профиль использует `quality: low` и не делает автоматический повтор submit
  при неопределённом результате, чтобы не получить двойное списание.

## Секрет

`GRSAI_API_KEY` хранится только в private `<HERMES_HOME>/.env` mode 0600.
Ключ не передаётся installer через argv и не хранится в GitHub.
Опционально можно задать `GRSAI_BASE_URL`; по умолчанию используется global host.

## Установка профиля

Сначала администратор безопасно добавляет `GRSAI_API_KEY` в private `.env`.
После этого из root-owned checkout:

```bash
python3 modules/grsai-image-provider/install.py --owner <owner>
```

Installer создаёт backup конфигурации, устанавливает user-scoped backend,
убирает `image_gen` из disabled toolsets, выбирает `grsai/gpt-image-2.5`
и сообщает `restart_required=true`. Gateway перезапускается отдельно после
проверки конкретного профиля.

## Приёмка

1. Проверить, что активный image provider равен `grsai` и `is_available=true`.
2. Выполнить один text-to-image smoke.
3. Выполнить один reference-image edit на неперсональном тестовом изображении.
4. Проверить локальный файл результата и отсутствие ключа в логах/Git diff.
5. Только затем включать следующий профиль.
