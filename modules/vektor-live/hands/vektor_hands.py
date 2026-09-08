"""Руки ВЕКТОРА на маке.

Соединение исходящее: агент сам подключается к сервису на VPS и ждёт команд.
Входящих портов не открывает, поэтому не требует ни проброса, ни доверия к сети.

Что умеет: снять экран, подсветить точку, кликнуть, напечатать, нажать сочетание,
открыть ссылку. Режим приходит с каждой командой и проверяется здесь ещё раз —
сервер и агент не доверяют друг другу на слово.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import tempfile

import certifi
import websockets
from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE / "scripts"
JXA = ["osascript", "-l", "JavaScript"]

def _load_secrets() -> None:
    """Ключ лежит в файле секретов; в окружении он не обязателен."""
    path = pathlib.Path.home() / ".claude" / "secrets" / "vektor-live.env"
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_secrets()
SERVER = os.environ.get("VEKTOR_HANDS_URL", "wss://vektor.srv1250550.hstgr.cloud/hands")
KEY = os.environ.get("VEKTOR_HANDS_KEY", "")
SHOT_WIDTH = 1280            # ширина кадра, в этих пикселях модель называет координаты

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("hands")

# Что разрешено в каком режиме. watch — руки полностью выключены.
ALLOWED = {
    "watch": {"screenshot", "zoom", "find"},
    "hint": {"screenshot", "zoom", "find", "point"},
    "act": {"screenshot", "zoom", "find", "point", "click", "type", "key", "open_url"},
}

GRID = 100          # шаг координатной сетки в пикселях кадра
_last_shot: dict = {}   # полноразмерный снимок для увеличения: путь и размеры


def run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "команда не уложилась во время"


def screen_size() -> tuple[int, int]:
    """Логический размер экрана в пунктах — в них же работают клики и Accessibility."""
    code, out = run(["osascript", "-e",
                     'tell application "Finder" to get bounds of window of desktop'])
    if code == 0 and out:
        parts = [int(x.strip()) for x in out.split(",")]
        if len(parts) == 4:
            return parts[2], parts[3]
    return 0, 0


def take_screenshot() -> dict:
    """Снимок экрана с координатной сеткой. Полноразмерный оригинал остаётся для увеличения."""
    global _last_shot
    old = _last_shot.get("dir")
    workdir = pathlib.Path(tempfile.mkdtemp())
    full = workdir / "full.png"
    code, out = run(["screencapture", "-x", "-C", str(full)], timeout=25)
    if code != 0 or not full.exists():
        shutil.rmtree(workdir, ignore_errors=True)
        return {"ok": False, "error": f"снимок не получился: {out[:150]}"}

    img = Image.open(full).convert("RGB")
    fw, fh = img.size
    frame = img.copy()
    frame.thumbnail((SHOT_WIDTH, SHOT_WIDTH * 10), Image.LANCZOS)
    w, h = frame.size

    # сетка: по ней модель называет координаты гораздо точнее, чем на глаз
    grid = frame.copy()
    d = ImageDraw.Draw(grid, "RGBA")
    try:
        font = ImageFont.load_default(13)
    except TypeError:      # старые Pillow не принимают размер
        font = ImageFont.load_default()
    for x in range(GRID, w, GRID):
        d.line([(x, 0), (x, h)], fill=(255, 80, 40, 90), width=1)
        d.text((x + 3, 2), str(x), fill=(255, 255, 255, 255), font=font,
               stroke_width=2, stroke_fill=(0, 0, 0, 220))
    for y in range(GRID, h, GRID):
        d.line([(0, y), (w, y)], fill=(255, 80, 40, 90), width=1)
        d.text((3, y + 2), str(y), fill=(255, 255, 255, 255), font=font,
               stroke_width=2, stroke_fill=(0, 0, 0, 220))

    shot = workdir / "grid.jpg"
    grid.save(shot, "JPEG", quality=80)
    img.save(workdir / "orig.png")
    _last_shot = {"dir": workdir, "path": workdir / "orig.png",
                  "full_w": fw, "full_h": fh, "frame_w": w, "frame_h": h}
    if old and old != workdir:
        shutil.rmtree(old, ignore_errors=True)

    sw, sh = screen_size()
    return {"ok": True, "image": base64.b64encode(shot.read_bytes()).decode(),
            "width": w, "height": h, "screen_width": sw, "screen_height": sh}


def zoom_at(x: float, y: float, span: int = 220) -> dict:
    """Кусок последнего снимка вокруг точки в полном разрешении, с перекрестием ровно в ней.
    Это проверка перед нажатием: модель видит, во что реально упирается её координата."""
    if not _last_shot or not _last_shot["path"].exists():
        return {"ok": False, "error": "сначала сделай снимок экрана"}
    img = Image.open(_last_shot["path"]).convert("RGB")
    k = _last_shot["full_w"] / _last_shot["frame_w"]
    cx, cy = x * k, y * k
    half = span * k / 2
    box = (max(0, int(cx - half)), max(0, int(cy - half)),
           min(img.width, int(cx + half)), min(img.height, int(cy + half)))
    crop = img.crop(box)
    d = ImageDraw.Draw(crop)
    px, py = cx - box[0], cy - box[1]
    d.line([(px - 26, py), (px + 26, py)], fill=(255, 60, 30), width=3)
    d.line([(px, py - 26), (px, py + 26)], fill=(255, 60, 30), width=3)
    d.ellipse([px - 13, py - 13, px + 13, py + 13], outline=(255, 60, 30), width=3)
    buf = pathlib.Path(tempfile.mkdtemp()) / "zoom.jpg"
    crop.save(buf, "JPEG", quality=88)
    data = base64.b64encode(buf.read_bytes()).decode()
    shutil.rmtree(buf.parent, ignore_errors=True)
    return {"ok": True, "image": data,
            "note": (f"Увеличенный кусок вокруг точки {round(x)},{round(y)}. "
                     "Перекрестие — ровно та точка. Если оно не на нужном элементе, "
                     "поправь координаты и проверь снова.")}


def find_elements(text: str, app: str = "") -> dict:
    """Поиск по видимой подписи через Accessibility. Даёт точные координаты без угадывания."""
    script = SCRIPTS / "axfind.applescript"
    code, out = run(["perl", "-e", "alarm 30; exec @ARGV", "osascript", str(script), text, app],
                    timeout=35)
    if code != 0:
        return {"ok": False, "error": f"поиск не удался: {out[:150]}"}
    lines = [ln for ln in out.splitlines() if ln.strip()]
    app_name = lines[0][4:] if lines and lines[0].startswith("APP:") else app
    if "NOWINDOW" in out or len(lines) < 2:
        return {"ok": True, "app": app_name, "matches": [],
                "note": (f"В «{app_name}» ничего с такой подписью не нашлось. "
                         "Браузеры на движке Chromium своё дерево не отдают — там работай "
                         "по координатам с сетки и обязательно проверь увеличением.")}
    matches = []
    for ln in lines[1:]:
        parts = ln.split("|")
        if len(parts) == 6:
            matches.append({"role": parts[0], "name": parts[1],
                            "screen_x": int(parts[2]), "screen_y": int(parts[3]),
                            "w": int(parts[4]), "h": int(parts[5])})
    return {"ok": True, "app": app_name, "matches": matches[:8],
            "note": "Координаты screen_x/screen_y — уже точки экрана. Передавай их как есть, "
                    "в поле screen_x/screen_y, не пересчитывай."}


def to_screen(x: float, y: float, frame_w: int, frame_h: int) -> tuple[float, float]:
    """Координаты кадра → пункты экрана."""
    sw, sh = screen_size()
    if not frame_w or not frame_h or not sw or not sh:
        return x, y
    return x * sw / frame_w, y * sh / frame_h


def focused_is_secure() -> bool:
    """Проверяем, не стоит ли курсор в поле пароля — в такие поля не печатаем никогда."""
    code, out = run(["osascript", "-e", '''
        tell application "System Events"
          try
            set p to first application process whose frontmost is true
            set e to value of attribute "AXFocusedUIElement" of p
            return role of e
          on error
            return "unknown"
          end try
        end tell'''], timeout=8)
    return "AXSecureTextField" in out


async def execute(msg: dict) -> dict:
    action = msg.get("action", "")
    mode = msg.get("mode", "watch")
    if action not in ALLOWED.get(mode, set()):
        return {"ok": False, "error": f"в режиме «{mode}» действие «{action}» запрещено"}

    if action == "screenshot":
        return await asyncio.to_thread(take_screenshot)

    if action == "zoom":
        return await asyncio.to_thread(zoom_at, float(msg.get("x", 0)), float(msg.get("y", 0)),
                                       int(msg.get("span", 220)))

    if action == "find":
        return await asyncio.to_thread(find_elements, str(msg.get("text", "")), str(msg.get("app", "")))

    fw = int(msg.get("frame_width") or 0)
    fh = int(msg.get("frame_height") or 0)

    def coords() -> tuple[float, float]:
        """Точные координаты из поиска идут как есть; координаты с кадра пересчитываем."""
        if msg.get("screen_x") is not None and msg.get("screen_y") is not None:
            return float(msg["screen_x"]), float(msg["screen_y"])
        return to_screen(float(msg.get("x", 0)), float(msg.get("y", 0)), fw, fh)

    if action == "point":
        x, y = coords()
        label = str(msg.get("label", ""))[:60]
        secs = str(min(8.0, float(msg.get("seconds", 3.0))))
        # не ждём: подсветка живёт своей жизнью, разговор не тормозится
        subprocess.Popen(JXA + [str(SCRIPTS / "highlight.js"), str(round(x)), str(round(y)), secs, label],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("подсветка (%.0f, %.0f) «%s»", x, y, label)
        return {"ok": True, "note": f"Подсветил точку {round(x)},{round(y)} на экране Павла."}

    if action == "click":
        x, y = coords()
        script = SCRIPTS / "jxaclick.js"
        if not script.exists():
            return {"ok": False, "error": f"на маке нет файла {script.name} — руки не собраны до конца"}
        code, out = await asyncio.to_thread(run, JXA + [str(script), str(x), str(y)])
        log.info("клик (%.0f, %.0f) → %s", x, y, out[:60])
        return {"ok": code == 0, "note": out[:200] if code == 0 else "",
                "error": "" if code == 0 else out[:200]}

    if action == "type":
        text = str(msg.get("text", ""))[:500]
        if await asyncio.to_thread(focused_is_secure):
            return {"ok": False, "error": "курсор стоит в поле пароля — сюда я не печатаю"}
        code, out = await asyncio.to_thread(
            run, ["osascript", "-e", 'on run a\ntell application "System Events" to keystroke (item 1 of a)\nend run', text])
        log.info("набор текста, %s символов", len(text))
        return {"ok": code == 0, "note": out[:200] or "напечатано"}

    if action == "key":
        combo = str(msg.get("combo", ""))[:60]
        mods = {"cmd": "command down", "shift": "shift down", "alt": "option down",
                "opt": "option down", "ctrl": "control down"}
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        if not parts:
            return {"ok": False, "error": "не понял сочетание"}
        keyname, using = parts[-1], [mods[p] for p in parts[:-1] if p in mods]
        special = {"enter": 36, "return": 36, "tab": 48, "space": 49, "esc": 53, "escape": 53,
                   "delete": 51, "backspace": 51, "up": 126, "down": 125, "left": 123, "right": 124}
        clause = f" using {{{', '.join(using)}}}" if using else ""
        if keyname in special:
            script = f'tell application "System Events" to key code {special[keyname]}{clause}'
        else:
            script = f'tell application "System Events" to keystroke "{keyname[:1]}"{clause}'
        code, out = await asyncio.to_thread(run, ["osascript", "-e", script])
        log.info("сочетание %s", combo)
        return {"ok": code == 0, "note": out[:200] or f"нажато {combo}"}

    if action == "open_url":
        url = str(msg.get("url", ""))
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "открываю только http и https"}
        code, out = await asyncio.to_thread(run, ["open", url])
        log.info("открыт адрес %s", url[:70])
        return {"ok": code == 0, "note": out[:200] or "открыто"}

    return {"ok": False, "error": f"неизвестное действие {action}"}


async def serve() -> None:
    if not KEY:
        log.error("нет ключа: задайте VEKTOR_HANDS_KEY")
        sys.exit(1)
    sw, sh = screen_size()
    url = f"{SERVER}?key={KEY}"
    # у сборки python с python.org нет системного хранилища корневых сертификатов
    ctx = ssl.create_default_context(cafile=certifi.where()) if url.startswith("wss://") else None
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=25, ping_timeout=60, max_size=None, ssl=ctx) as ws:
                log.info("подключён к ВЕКТОРУ, экран %sx%s пунктов", sw, sh)
                await ws.send(json.dumps({"type": "hello", "screen_width": sw, "screen_height": sh}))
                backoff = 1.0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    result = await execute(msg)
                    result["id"] = msg.get("id")
                    result["type"] = "result"
                    await ws.send(json.dumps(result))
        except Exception as exc:  # noqa: BLE001
            log.warning("связь потеряна (%s: %s) — переподключаюсь через %.0f с",
                        type(exc).__name__, str(exc)[:90], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    asyncio.run(serve())
