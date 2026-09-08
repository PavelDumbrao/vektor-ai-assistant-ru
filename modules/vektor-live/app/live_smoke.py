"""Короткий реальный тест Gemini Live API: setup -> текст -> аудио+транскрипт. Ключ не печатается."""
import asyncio, os, sys, time
from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")

async def main() -> int:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("нет ключа в окружении"); return 2
    client = genai.Client(api_key=key)
    cfg = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text="Отвечай по-русски, одним коротким предложением.")]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )
    t0 = time.monotonic()
    async with client.aio.live.connect(model=MODEL, config=cfg) as session:
        t_conn = time.monotonic() - t0
        print(f"  соединение установлено за {t_conn:.2f} с, модель {MODEL}")
        t1 = time.monotonic()
        await session.send_realtime_input(text="Скажи одним предложением, что ты на связи.")
        audio_bytes, first_audio, text_out = 0, None, []
        async for resp in session.receive():
            sc = resp.server_content
            if not sc:
                continue
            if sc.model_turn:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        if first_audio is None:
                            first_audio = time.monotonic() - t1
                        audio_bytes += len(part.inline_data.data)
            if sc.output_transcription and sc.output_transcription.text:
                text_out.append(sc.output_transcription.text)
            if sc.turn_complete:
                break
        secs = audio_bytes / 2 / 24000
        print(f"  время до первого аудио (TTFA): {first_audio:.2f} с" if first_audio else "  аудио не пришло")
        print(f"  аудио: {audio_bytes} байт PCM16@24k = {secs:.1f} с речи")
        print(f"  транскрипт ответа: {''.join(text_out).strip()[:200]}")
        return 0 if audio_bytes else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
