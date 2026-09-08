"""ВЕКТОР Live — локальный сервер: браузер ↔ Gemini Live ↔ существующий Hermes.

Слушает только 127.0.0.1, требует токен запуска, проверяет Host/Origin.
Ключи остаются на этой стороне и никогда не уходят во фронтенд.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Optional

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types

from . import config
from .hands_hub import MODE_NAMES, hub
from .hermes import HermesClient, HermesTask
from .links import LinkStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vektor")

app = FastAPI(title="ВЕКТОР Live", docs_url=None, redoc_url=None, openapi_url=None)
hermes = HermesClient()
links = LinkStore()

ALLOWED_HOSTS = config.ALLOWED_HOSTS

TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="ask_hermes",
            description=(
                "Обратиться к личному агенту Павла (Гермесу) за его личным контекстом, проектами, "
                "договорённостями, задачами и файлами — или поручить ему что-то сделать. "
                "Вызывай только когда нужен личный контекст Павла или реальное действие: обычный разговор "
                "и описание экрана делай сам. Возвращает task_id и статус awaiting_confirmation — "
                "Павел должен подтвердить отправку в интерфейсе. Скажи ему об этом и продолжай разговор."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "request": types.Schema(
                        type=types.Type.STRING,
                        description="Точная формулировка запроса или поручения для Гермеса, по-русски.",
                    ),
                    "screen_context": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Необязательно: короткое описание того, что сейчас видно на экране, "
                            "если это нужно для задачи. Одно-два предложения, не выгрузка текста."
                        ),
                    ),
                },
                required=["request"],
            ),
        ),
        types.FunctionDeclaration(
            name="look_at_screen",
            description=(
                "Взять свежий снимок экрана Павла и посмотреть на него. Кадры сами по себе не приходят — "
                "чтобы увидеть экран, всегда вызывай этот инструмент. Вызывай его молча, без вопросов, "
                "каждый раз когда разговор касается экрана, открытых окон, ошибок или того, куда нажать. "
                "После ответа инструмента кадр уже перед тобой — описывай то, что на нём."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "reason": types.Schema(
                        type=types.Type.STRING,
                        description="Коротко, зачем смотришь. Одна фраза, по-русски.",
                    ),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="point_at_screen",
            description=(
                "Подсветить место на экране Павла: поверх окон появляется кольцо с подписью. "
                "Курсор, фокус и его работу это не трогает — он видит, куда нажать, и жмёт сам. "
                "Координаты бери с последнего кадра look_at_screen, в его пикселях. "
                "Доступно в режимах «подсказывает» и «действует»."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "x": types.Schema(type=types.Type.NUMBER, description="X в пикселях кадра"),
                    "y": types.Schema(type=types.Type.NUMBER, description="Y в пикселях кадра"),
                    "label": types.Schema(type=types.Type.STRING,
                                          description="Короткая подпись рядом с кольцом, до 5 слов."),
                    "screen_x": types.Schema(type=types.Type.NUMBER,
                                             description="Точная координата из find_on_screen. Тогда x и y не нужны."),
                    "screen_y": types.Schema(type=types.Type.NUMBER,
                                             description="Точная координата из find_on_screen."),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="find_on_screen",
            description=(
                "Найти элемент на экране по его видимой подписи и получить ТОЧНЫЕ координаты. "
                "Это главный способ прицелиться: он не требует угадывать пиксели. "
                "Работает в обычных программах мака (Настройки, Finder, Telegram, почта). "
                "В браузерах на движке Chromium не работает — там придётся по сетке с проверкой. "
                "Найденные screen_x и screen_y передавай в point_at_screen и do_on_desktop как есть."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "text": types.Schema(type=types.Type.STRING,
                                         description="Кусок подписи элемента, как он написан на экране."),
                    "app": types.Schema(type=types.Type.STRING,
                                        description="Имя программы. Пусто — активная."),
                },
                required=["text"],
            ),
        ),
        types.FunctionDeclaration(
            name="look_closer",
            description=(
                "Увеличить кусок экрана вокруг точки: приходит кадр с перекрестием ровно в ней. "
                "Обязательный шаг перед любым нажатием по координатам с кадра: посмотри, "
                "на том ли элементе перекрестие, и поправь координаты, если промахнулся."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "x": types.Schema(type=types.Type.NUMBER, description="X в пикселях кадра"),
                    "y": types.Schema(type=types.Type.NUMBER, description="Y в пикселях кадра"),
                },
                required=["x", "y"],
            ),
        ),
        types.FunctionDeclaration(
            name="do_on_desktop",
            description=(
                "Сделать действие на маке Павла: нажать, напечатать, нажать сочетание клавиш, "
                "открыть ссылку. Работает только в режиме «действует». Перед вызовом одной фразой "
                "скажи вслух, что именно делаешь. Необратимое — отправку, оплату, удаление, "
                "публикацию — не делай: подсветь кнопку и попроси Павла нажать самому."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "action": types.Schema(type=types.Type.STRING,
                                           description="click, type, key или open_url"),
                    "x": types.Schema(type=types.Type.NUMBER, description="для click: X в пикселях кадра"),
                    "y": types.Schema(type=types.Type.NUMBER, description="для click: Y в пикселях кадра"),
                    "text": types.Schema(type=types.Type.STRING, description="для type: что напечатать"),
                    "combo": types.Schema(type=types.Type.STRING,
                                          description="для key: например cmd+s, enter, esc"),
                    "url": types.Schema(type=types.Type.STRING, description="для open_url: адрес"),
                    "screen_x": types.Schema(type=types.Type.NUMBER,
                                             description="для click: точная координата из find_on_screen"),
                    "screen_y": types.Schema(type=types.Type.NUMBER,
                                             description="для click: точная координата из find_on_screen"),
                },
                required=["action"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_hermes_task",
            description=(
                "Узнать состояние ранее подготовленного поручения по task_id: ждёт подтверждения, "
                "выполняется, готово или ошибка. Возвращает результат, когда он есть."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"task_id": types.Schema(type=types.Type.STRING)},
                required=["task_id"],
            ),
        ),
    ])
]


def live_config(resume_handle: Optional[str] = None) -> types.LiveConnectConfig:
    """Конфиг Live-сессии: русский голос, транскрипции, сжатие контекста и возобновление."""
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.GEMINI_VOICE)
            ),
            language_code="ru-RU",
        ),
        system_instruction=types.Content(parts=[types.Part(text=config.SYSTEM_INSTRUCTION)]),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
        session_resumption=types.SessionResumptionConfig(handle=resume_handle),
        tools=TOOLS,
    )


BRIDGES: set["Bridge"] = set()


async def _broadcast_hands() -> None:
    """Руки подключились или отпали — сообщаем всем открытым разговорам."""
    for bridge in list(BRIDGES):
        await bridge.status(hands="подключены" if hub.connected else "не подключены")


hub.on_change = _broadcast_hands


class Bridge:
    """Одна разговорная сессия: браузер ↔ Gemini Live."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.session: Any = None
        self.client = genai.Client(api_key=config.GEMINI_API_KEY)
        self.resume_handle: Optional[str] = None
        self.screen_on = False
        self.last_frame_at = 0.0
        self.last_activity = time.monotonic()
        self.closing = False
        self.pending_tool_ids: dict[str, str] = {}  # task_id -> function call id (для контекста)
        self.reconnects = 0
        self.frames_sent = 0
        self.context_loaded = False
        # учёт расхода: складываем токены по модальностям за всю сессию
        self.usage_in: dict[str, int] = {}
        self.usage_out: dict[str, int] = {}
        self.usage_total = 0
        # ожидание кадра, запрошенного инструментом look_at_screen
        self.frame_event = asyncio.Event()
        self.frame_on_demand = False
        # режим разговора: watch — только смотрит, hint — подсвечивает, act — действует
        self.mode = "watch"
        # размеры последнего кадра: в этих пикселях модель называет координаты
        self.frame_w = 0
        self.frame_h = 0

    # ---------- отправка клиенту ----------

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.closing:
            return
        with contextlib.suppress(Exception):
            await self.ws.send_json(payload)

    async def send_audio(self, data: bytes) -> None:
        if self.closing:
            return
        with contextlib.suppress(Exception):
            await self.ws.send_bytes(data)

    async def status(self, **kwargs: Any) -> None:
        await self.send_json({"type": "status", **kwargs})

    # ---------- расход ----------

    def _cost(self) -> float:
        """Стоимость сессии в долларах по прайсу Live API."""
        total = 0.0
        for side, table in (("input", self.usage_in), ("output", self.usage_out)):
            prices = config.PRICE_PER_MTOK[side]
            for modality, tokens in table.items():
                # незнакомая модальность считается по самой дорогой ставке этой стороны,
                # чтобы счёт никогда не выглядел меньше реального
                price = prices.get(modality, max(prices.values()))
                total += tokens * price / 1_000_000
        return total

    async def record_usage(self, meta: Any) -> None:
        """Складывает usage_metadata очередного ответа. Live API тарифицирует каждый ход целиком."""
        def add(table: dict[str, int], details: Any, fallback: int) -> None:
            counted = 0
            for item in details or []:
                modality = getattr(item, "modality", None)
                name = getattr(modality, "name", None) or str(modality or "TEXT")
                name = name.rsplit(".", 1)[-1].upper()
                count = int(getattr(item, "token_count", 0) or 0)
                table[name] = table.get(name, 0) + count
                counted += count
            if not counted and fallback:
                table["TEXT"] = table.get("TEXT", 0) + int(fallback)

        add(self.usage_in, getattr(meta, "prompt_tokens_details", None),
            getattr(meta, "prompt_token_count", 0) or 0)
        add(self.usage_out, getattr(meta, "response_tokens_details", None),
            getattr(meta, "response_token_count", 0) or 0)
        self.usage_total += int(getattr(meta, "total_token_count", 0) or 0)

        usd = self._cost()
        text = f"${usd:.3f}"
        if config.USD_RUB:
            text += f" (~{usd * config.USD_RUB:.0f} ₽)"
        detail = ("вход: " + ", ".join(f"{k} {v}" for k, v in sorted(self.usage_in.items()))
                  + " | выход: " + ", ".join(f"{k} {v}" for k, v in sorted(self.usage_out.items())))
        await self.send_json({"type": "usage", "text": text, "detail": detail,
                              "usd": round(usd, 4), "tokens": self.usage_total})

    # ---------- инструменты ----------

    async def handle_tool_call(self, tool_call: Any) -> None:
        responses = []
        for fc in tool_call.function_calls or []:
            name = fc.name
            args = dict(fc.args or {})
            if name == "ask_hermes":
                task = hermes.create(args.get("request", ""), args.get("screen_context", ""))
                self.pending_tool_ids[task.task_id] = fc.id or ""
                await self.send_json({"type": "task", "card": hermes.to_card(task)})
                log.info("ask_hermes → %s (ждёт подтверждения)", task.task_id)
                result = {
                    "task_id": task.task_id,
                    "status": "awaiting_confirmation",
                    "note": ("Запрос показан Павлу карточкой в интерфейсе. Он должен нажать "
                             "«Передать Гермесу». Скажи ему об этом и продолжай разговор; "
                             "результат придёт позже."),
                }
            elif name == "look_at_screen":
                result = await self.grab_frame(args.get("reason", ""))
            elif name == "find_on_screen":
                result = await hub.call("find", self.mode, text=args.get("text", ""),
                                        app=args.get("app", ""), timeout=40)
                log.info("поиск «%s» → %s совпадений", str(args.get("text"))[:40],
                         len(result.get("matches") or []))
            elif name == "look_closer":
                result = await self.zoom_frame(args.get("x", 0), args.get("y", 0))
            elif name == "point_at_screen":
                result = await hub.call(
                    "point", self.mode, x=args.get("x", 0), y=args.get("y", 0),
                    label=args.get("label", ""), seconds=3.5,
                    frame_width=self.frame_w, frame_height=self.frame_h)
                if result.get("ok"):
                    log.info("подсветка (%s, %s) «%s»", args.get("x"), args.get("y"), args.get("label", "")[:40])
            elif name == "do_on_desktop":
                sub = str(args.get("action", "")).strip().lower()
                params = {k: v for k, v in args.items() if k != "action" and v is not None}
                result = await hub.call(sub, self.mode, frame_width=self.frame_w,
                                        frame_height=self.frame_h, **params)
                log.info("действие «%s» в режиме %s → %s", sub, self.mode,
                         "ок" if result.get("ok") else str(result.get("error"))[:80])
            elif name == "get_hermes_task":
                task = hermes.get(args.get("task_id", ""))
                if not task:
                    result = {"status": "unknown", "note": "такого поручения нет"}
                else:
                    result = {"task_id": task.task_id, "status": task.status,
                              "result": task.result[:4000], "error": task.error}
                    if task.status in {"done", "error"}:
                        task.delivered_to_model = True
            else:
                result = {"error": f"неизвестный инструмент {name}"}
            responses.append(types.FunctionResponse(id=fc.id, name=name, response=result))
        if responses and self.session:
            with contextlib.suppress(Exception):
                await self.session.send_tool_response(function_responses=responses)

    async def grab_frame(self, reason: str) -> dict[str, Any]:
        """Просит браузер снять экран прямо сейчас и ждёт, пока кадр дойдёт до модели."""
        log.info("look_at_screen: %s", reason[:80] or "без пояснения")
        # если на маке запущен агент, кадр берём у него: тогда координаты для подсветки
        # и нажатий совпадают с реальным экраном пиксель в пиксель
        if hub.connected:
            shot = await hub.call("screenshot", self.mode)
            if shot.get("ok") and shot.get("image") and self.session:
                self.frame_w, self.frame_h = int(shot["width"]), int(shot["height"])
                with contextlib.suppress(Exception):
                    await self.session.send_realtime_input(
                        video=types.Blob(data=base64.b64decode(shot["image"]), mime_type="image/jpeg")
                    )
                self.frames_sent += 1
                await self.status(frame_at=time.time(), frames=self.frames_sent)
                await self.send_json({"type": "shot", "image": shot["image"]})
                return {"status": "ok",
                        "note": (f"Кадр экрана перед тобой, размер {self.frame_w}x{self.frame_h} пикселей, "
                                 "на нём координатная сетка с шагом 100. Координаты называй в этих пикселях. "
                                 + self.mode_brief())}
            log.warning("руки не отдали кадр: %s", str(shot.get("error"))[:120])

        if not self.screen_on:
            return {"status": "screen_off",
                    "note": ("Экран сейчас не виден: агент на маке не запущен и показ через браузер "
                             "выключен. Скажи об этом Павлу.")}
        self.frame_event.clear()
        self.frame_on_demand = True
        await self.send_json({"type": "request_frame"})
        try:
            await asyncio.wait_for(self.frame_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            self.frame_on_demand = False
            return {"status": "timeout",
                    "note": "Кадр не пришёл за 4 секунды. Скажи Павлу, что снимок не получился."}
        return {"status": "ok", "note": "Свежий кадр экрана уже перед тобой — опиши, что на нём."}

    def mode_brief(self) -> str:
        """Что именно разрешено в текущем режиме — перечисляем поимённо, а не намёком."""
        common = "look_at_screen, look_closer, find_on_screen, ask_hermes"
        if self.mode == "watch":
            return (f"Сейчас режим «смотрит». Доступны: {common}. Подсвечивать и нажимать нельзя — "
                    "если нужно показать место, попроси Павла включить режим «подсказывает».")
        if self.mode == "hint":
            return (f"Сейчас режим «подсказывает». Доступны: {common} и point_at_screen. "
                    "Подсветку делай сразу, разрешения не спрашивай и переключить режим ради неё не проси. "
                    "Нажимать нельзя — нажимает Павел сам.")
        return (f"Сейчас режим «действует». Доступны: {common}, point_at_screen и do_on_desktop. "
                "Нажимай и печатай сам, но необратимое всё равно оставляй Павлу.")

    async def announce_mode(self) -> None:
        if not self.session:
            return
        with contextlib.suppress(Exception):
            await self.session.send_realtime_input(
                text=f"[Системное сообщение] {self.mode_brief()} Вслух это не проговаривай."
            )

    async def zoom_frame(self, x: float, y: float) -> dict[str, Any]:
        """Увеличенный кусок вокруг точки — модель проверяет прицел до нажатия."""
        shot = await hub.call("zoom", self.mode, x=x, y=y)
        if not shot.get("ok") or not shot.get("image") or not self.session:
            return {"status": "error", "note": shot.get("error", "увеличить не вышло")}
        with contextlib.suppress(Exception):
            await self.session.send_realtime_input(
                video=types.Blob(data=base64.b64decode(shot["image"]), mime_type="image/jpeg")
            )
        await self.send_json({"type": "shot", "image": shot["image"]})
        return {"status": "ok", "note": shot.get("note", "")}

    async def deliver_task_result(self, task: HermesTask) -> None:
        """Готовый результат отдаём отдельным текстовым событием в паузе, а не вторым function response."""
        await self.send_json({"type": "task", "card": hermes.to_card(task)})
        if task.status not in {"done", "error"} or task.delivered_to_model or not self.session:
            return
        if task.status == "done":
            text = (f"[Системное сообщение] Гермес выполнил поручение {task.task_id}. "
                    f"Результат:\n{task.result[:4000]}\n"
                    f"Коротко перескажи это Павлу своими словами.")
        else:
            text = (f"[Системное сообщение] Поручение {task.task_id} не выполнено: {task.error[:300]}. "
                    f"Сообщи Павлу об ошибке коротко.")
        with contextlib.suppress(Exception):
            await self.session.send_realtime_input(text=text)
            task.delivered_to_model = True

    # ---------- приём из Gemini ----------

    async def pump_gemini(self) -> None:
        """Читает события Gemini. receive() завершается после каждого хода — поэтому цикл снаружи."""
        assert self.session is not None
        while not self.closing:
            await self._pump_one_turn()

    async def _pump_one_turn(self) -> None:
        assert self.session is not None
        async for resp in self.session.receive():
            self.last_activity = time.monotonic()

            meta = getattr(resp, "usage_metadata", None)
            if meta:
                await self.record_usage(meta)

            if getattr(resp, "session_resumption_update", None):
                upd = resp.session_resumption_update
                if upd.resumable and upd.new_handle:
                    self.resume_handle = upd.new_handle

            if getattr(resp, "go_away", None):
                left = getattr(resp.go_away, "time_left", None)
                log.info("Gemini GoAway, осталось %s — переподключаюсь", left)
                await self.status(gemini="переподключение")
                raise _Reconnect()

            if getattr(resp, "tool_call", None):
                await self.handle_tool_call(resp.tool_call)
                continue

            sc = resp.server_content
            if not sc:
                continue

            if sc.interrupted:
                await self.send_json({"type": "interrupted"})

            if sc.model_turn and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    # мысли и служебный текст не озвучиваем и не показываем как ответ
                    if getattr(part, "thought", None):
                        continue
                    if part.inline_data and part.inline_data.data:
                        await self.send_audio(part.inline_data.data)

            if sc.input_transcription and sc.input_transcription.text:
                await self.send_json({"type": "transcript", "role": "user",
                                      "text": sc.input_transcription.text})
                # клиенту: пользователь заговорил — в режиме «по запросу» пора слать кадр
                await self.send_json({"type": "user_spoke"})
            if sc.output_transcription and sc.output_transcription.text:
                await self.send_json({"type": "transcript", "role": "vektor",
                                      "text": sc.output_transcription.text})
            if sc.turn_complete:
                await self.send_json({"type": "turn_complete"})

    # ---------- приём от браузера ----------

    async def pump_client(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(msg.get("code", 1000))
            self.last_activity = time.monotonic()

            data = msg.get("bytes")
            if data:  # аудио с микрофона: PCM16 mono 16 кГц
                if self.session:
                    with contextlib.suppress(Exception):
                        await self.session.send_realtime_input(
                            audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                        )
                continue

            text = msg.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            await self.handle_client_message(payload)

    async def handle_client_message(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")

        if kind == "text" and self.session:
            body = (payload.get("text") or "").strip()
            if body:
                await self.send_json({"type": "transcript", "role": "user", "text": body})
                with contextlib.suppress(Exception):
                    await self.session.send_realtime_input(text=body)

        elif kind == "frame" and self.session:
            raw = payload.get("data") or ""
            if not raw or not self.screen_on:
                return
            now = time.monotonic()
            # кадры теперь приходят по запросу, поток снят; лёгкий предохранитель от спама
            if not self.frame_on_demand and now - self.last_frame_at < 0.3:
                return
            self.last_frame_at = now
            try:
                blob = base64.b64decode(raw)
                self.frame_w = int(payload.get("width") or 0)
                self.frame_h = int(payload.get("height") or 0)
                await self.session.send_realtime_input(
                    video=types.Blob(data=blob, mime_type="image/jpeg")
                )
                self.frames_sent += 1
                if self.frames_sent in (1, 5, 30) or self.frames_sent % 60 == 0:
                    log.info("кадр экрана №%s отправлен (%s КБ)", self.frames_sent, len(blob) // 1024)
                await self.status(frame_at=time.time(), frames=self.frames_sent)
                self.frame_on_demand = False
                self.frame_event.set()
            except Exception as exc:  # noqa: BLE001 — молча терять зрение нельзя
                log.warning("кадр экрана не ушёл: %s: %s", type(exc).__name__, str(exc)[:200])
                await self.status(screen_error=f"{type(exc).__name__}: {str(exc)[:120]}")

        elif kind == "image" and self.session:
            raw = payload.get("data") or ""
            if not raw:
                return
            try:
                blob = base64.b64decode(raw)
                await self.session.send_realtime_input(
                    video=types.Blob(data=blob, mime_type="image/jpeg")
                )
                name = str(payload.get("name") or "")[:80]
                await self.session.send_realtime_input(
                    text=("[Системное сообщение] Павел прислал картинку"
                          + (f" «{name}»" if name else "")
                          + ". Она перед тобой — посмотри и ответь по ней.")
                )
                log.info("картинка от Павла: %s КБ%s", len(blob) // 1024, f" ({name})" if name else "")
            except Exception as exc:  # noqa: BLE001
                log.warning("картинка не ушла: %s: %s", type(exc).__name__, str(exc)[:150])
                await self.send_json({"type": "status", "detail": "картинка не дошла до модели"})

        elif kind == "mode":
            new = str(payload.get("mode", "watch"))
            if new in MODE_NAMES:
                self.mode = new
                await self.status(mode=new, mode_name=MODE_NAMES[new])
                log.info("режим переключён: %s", MODE_NAMES[new])
                await self.announce_mode()

        elif kind == "frame_unavailable":
            self.frame_on_demand = False
            self.frame_event.set()

        elif kind == "screen_on":
            self.screen_on = True
            self.last_frame_at = 0.0
            await self.status(screen=True, source=payload.get("source", ""))
            # текстовое уведомление модели не шлём: оно вызывает лишний ход ответа
            # и сбивает разговор. Кадры говорят сами за себя.

        elif kind == "screen_off":
            self.screen_on = False
            await self.status(screen=False)
            if self.session:
                with contextlib.suppress(Exception):
                    await self.session.send_realtime_input(
                        text=("[Системное сообщение] Показ экрана выключен. Текущий экран тебе больше не "
                              "виден: не описывай его и не выдавай прежние кадры за актуальные.")
                    )

        elif kind == "confirm":
            task = await hermes.confirm_and_run(payload.get("task_id", ""))
            if task:
                await self.send_json({"type": "task", "card": hermes.to_card(task)})

        elif kind == "cancel":
            task = hermes.cancel(payload.get("task_id", ""))
            if task:
                await self.send_json({"type": "task", "card": hermes.to_card(task)})
                if self.session and task.status == "cancelled":
                    with contextlib.suppress(Exception):
                        await self.session.send_realtime_input(
                            text=f"[Системное сообщение] Павел отменил поручение {task.task_id}."
                        )

    async def preload_context(self) -> None:
        """Тихо спрашивает у Гермеса свежую сводку и кладёт её в начало разговора."""
        self.context_loaded = True
        task = hermes.create(
            "Кратко, по-русски: мои текущие проекты, ближайшие задачи и недавние договорённости. "
            "Только суть, без вступлений, до 12 строк."
        )
        # подтверждение не нужно — это чтение личного контекста владельца
        task.status = "running"
        task.started_at = time.time()
        await self._notify_hermes_start()
        await hermes._run(task)  # noqa: SLF001 — намеренно тот же путь, что и у подтверждённых
        if task.status == "done" and task.result and self.session:
            with contextlib.suppress(Exception):
                await self.session.send_realtime_input(
                    text=("[Системное сообщение] Свежая сводка от Гермеса о делах Павла — "
                          "используй её как контекст, не зачитывай вслух целиком:\n"
                          f"{task.result[:4000]}")
                )
            log.info("контекст Гермеса подгружен при старте (%s символов)", len(task.result))
        else:
            log.warning("контекст при старте не подгрузился: %s", task.error[:120] or task.status)
        await self.greet()

    async def greet(self) -> None:
        """Первый ход делает ВЕКТОР: здоровается по имени и называет главное на сегодня."""
        if not self.session:
            return
        with contextlib.suppress(Exception):
            await self.session.send_realtime_input(
                text=("[Системное сообщение] Разговор начался. Поздоровайся с Павлом по имени одной "
                      "фразой, одним предложением скажи главное на сегодня из сводки Гермеса и спроси, "
                      "продолжаем ли. Если сводки нет — просто поздоровайся и спроси, чем помочь.")
            )

    async def _notify_hermes_start(self) -> None:
        await self.status(context="загружается")

    # ---------- жизненный цикл ----------

    async def idle_watch(self) -> None:
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - self.last_activity > config.IDLE_TIMEOUT_SECONDS:
                await self.status(gemini="закрыто по простою")
                raise _Stop()

    async def run(self) -> None:
        hermes.on_update = self.deliver_task_result
        backoff = 1.0
        while not self.closing:
            try:
                async with self.client.aio.live.connect(
                    model=config.GEMINI_LIVE_MODEL,
                    config=live_config(self.resume_handle),
                ) as session:
                    self.session = session
                    backoff = 1.0
                    await self.status(gemini="подключено", model=config.GEMINI_LIVE_MODEL,
                                      voice=config.GEMINI_VOICE, reconnects=self.reconnects)
                    log.info("Live-сессия открыта (%s, голос %s)", config.GEMINI_LIVE_MODEL, config.GEMINI_VOICE)
                    # личный контекст подтягиваем сразу, без вопросов и подтверждений:
                    # это чтение данных самого Павла в его же голосовом режиме
                    await self.announce_mode()
                    if not self.context_loaded:
                        asyncio.create_task(self.preload_context())
                    # результаты, не доехавшие до модели (например, после перезапуска)
                    for task in hermes.undelivered():
                        await self.deliver_task_result(task)
                    tasks = [
                        asyncio.create_task(self.pump_gemini()),
                        asyncio.create_task(self.pump_client()),
                        asyncio.create_task(self.idle_watch()),
                    ]
                    try:
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                        for t in pending:
                            t.cancel()
                        for t in done:
                            exc = t.exception()
                            if exc:
                                raise exc
                    finally:
                        for t in tasks:
                            t.cancel()
            except (_Stop, WebSocketDisconnect):
                break
            except _Reconnect:
                self.reconnects += 1
                await self.send_json({"type": "interrupted"})
                continue
            except Exception as exc:  # noqa: BLE001
                self.reconnects += 1
                detail = f"{type(exc).__name__}: {str(exc)[:200]}"
                log.warning("Live-сессия оборвалась: %s", detail)
                await self.status(gemini="ошибка", detail=detail)
                await self.send_json({"type": "interrupted"})
                if self.reconnects > 5:
                    await self.status(gemini="остановлено: слишком много ошибок")
                    break
                await asyncio.sleep(min(backoff, 15))
                backoff *= 2
            finally:
                self.session = None
        self.closing = True
        hermes.on_update = None


class _Reconnect(Exception):
    """Сервер попросил переподключиться (GoAway)."""


class _Stop(Exception):
    """Плановая остановка сессии."""



def _static_version() -> str:
    """Версия статики = отпечаток app.js. Меняется при каждом деплое, поэтому
    браузер гарантированно берёт свежий файл, а не старый из кэша."""
    try:
        data = (config.WEB_DIR / "app.js").read_bytes()
        return hashlib.sha256(data).hexdigest()[:10]
    except OSError:
        return "0"


def _index_html() -> str:
    html = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
    v = _static_version()
    return html.replace("app.js", f"app.js?v={v}").replace("style.css", f"style.css?v={v}")


# ---------- HTTP ----------


def _host_ok(host: str) -> bool:
    return (host or "").lower().split(",")[0].strip() in ALLOWED_HOSTS


def _authorized(token: str, key: str) -> bool:
    """Пускаем либо по токену запуска (локальный режим), либо по живой ссылке от ВЕКТОРА."""
    if key and links.check(key):
        return True
    return bool(token) and token == config.SESSION_TOKEN


@app.get("/")
async def root(request: Request, token: str = Query(default=""), k: str = Query(default="")) -> Any:
    if not _host_ok(request.headers.get("host") or ""):
        return JSONResponse({"error": "неизвестный адрес"}, status_code=403)
    if not _authorized(token, k):
        return FileResponse(config.WEB_DIR / "expired.html", status_code=403)
    if k:
        links.mark_open(k, (request.headers.get("user-agent") or "")[:120])
    return HTMLResponse(_index_html(), headers={"Cache-Control": "no-store"})


@app.post("/api/link")
async def make_link(request: Request) -> Any:
    """ВЕКТОР (Гермес) просит новую ссылку на разговор и присылает её Павлу."""
    key = (request.headers.get("x-vektor-key") or "").strip()
    if not config.ADMIN_KEY or key != config.ADMIN_KEY:
        return JSONResponse({"error": "нет доступа"}, status_code=403)
    body: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        body = await request.json()
    link = links.create(minutes=body.get("minutes"), note=str(body.get("note") or ""))
    log.info("выдана ссылка на разговор (живёт %s мин)", config.LINK_TTL_MINUTES)
    return {
        "url": links.url_for(link),
        "expires_in_minutes": round((link.expires_at - time.time()) / 60),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    h = await hermes.health()
    return {
        "ok": True,
        "model": config.GEMINI_LIVE_MODEL,
        "voice": config.GEMINI_VOICE,
        "gemini_key": bool(config.GEMINI_API_KEY),
        "hermes": h,
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(default=""), k: str = Query(default="")) -> None:
    origin = (ws.headers.get("origin") or "").lower()
    host = (ws.headers.get("host") or "").lower()
    allowed_origins = {f"http://{h}" for h in ALLOWED_HOSTS} | {f"https://{h}" for h in ALLOWED_HOSTS}
    if not _authorized(token, k) or not _host_ok(host) or (origin and origin not in allowed_origins):
        await ws.close(code=4403)
        return
    await ws.accept()
    bridge = Bridge(ws)
    BRIDGES.add(bridge)
    h = await hermes.health()
    await bridge.status(hermes="доступен" if h["ok"] else "недоступен", hermes_detail=h["detail"],
                        hands="подключены" if hub.connected else "не подключены",
                        mode=bridge.mode, mode_name=MODE_NAMES[bridge.mode])
    try:
        await bridge.run()
    except WebSocketDisconnect:
        pass
    finally:
        BRIDGES.discard(bridge)
        bridge.closing = True
        with contextlib.suppress(Exception):
            await ws.close()
        log.info("сессия закрыта")


@app.websocket("/hands")
async def hands_endpoint(ws: WebSocket, key: str = Query(default="")) -> None:
    """Агент с мака Павла: подключается сам, входящих портов на маке не нужно."""
    if not config.HANDS_KEY or not secrets.compare_digest(key, config.HANDS_KEY):
        await ws.close(code=4403)
        return
    await ws.accept()
    info: dict[str, Any] = {}
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if first.get("type") == "hello":
            info = first
        await hub.attach(ws, info)
        while True:
            msg = await ws.receive_json()
            hub.resolve(msg)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("руки отвалились: %s: %s", type(exc).__name__, str(exc)[:150])
    finally:
        await hub.detach(ws)
        with contextlib.suppress(Exception):
            await ws.close()


app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")
