"""Test 7: STT transcription with model ladder, base64 input_audio, fallback on 500 / empty text, and response formats."""

import base64
import os
import tempfile
import unittest
from aiohttp import ClientSession, FormData, test_utils, web

from tests.test_helpers import MockUpstream, create_test_broker


class TestSTTTranscription(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mock_upstream = MockUpstream()
        os.environ["STT_API_KEY"] = "test-stt-key-openrouter"

    async def asyncTearDown(self):
        if hasattr(self, "app"):
            await self.app.stop()
        await self.mock_upstream.stop()
        self.tmpdir.cleanup()
        os.environ.pop("STT_API_KEY", None)

    async def test_stt_multipart_ladder_fallback(self):
        recorded_upstream_calls: list[dict] = []
        mode = "fail_500"  # or "empty_text"

        async def stt_handler(request: web.Request):
            body = await request.json()
            recorded_upstream_calls.append(body)
            model = body.get("model")

            if model == "model-A":
                if mode == "fail_500":
                    return web.Response(status=500, text="Internal Server Error")
                elif mode == "empty_text":
                    return web.json_response({"text": ""})
            elif model == "model-B":
                return web.json_response({"text": "привет"})

            return web.Response(status=400, text="Unknown model")

        self.mock_upstream.app.router.add_post("/v1/audio/transcriptions", stt_handler)
        upstream_url = await self.mock_upstream.start()
        lane_port = test_utils.unused_port()

        lanes = [{"client": "bebov", "uid": 1009, "port": lane_port}]
        routes = [
            {
                "prefix": "/stt/v1",
                "upstream": upstream_url + "/v1",
                "kind": "stt",
                "auth": {
                    "header": "Authorization",
                    "format": "Bearer {key}",
                    "key_env": "STT_API_KEY",
                },
                "ladder": ["model-A", "model-B"],
            }
        ]

        self.app, _, _ = create_test_broker(self.tmpdir.name, lanes, routes)
        await self.app.start()

        audio_bytes = b"OGG_TEST_AUDIO_PAYLOAD_12345"

        async with ClientSession() as session:
            url = f"http://127.0.0.1:{lane_port}/stt/v1/audio/transcriptions"

            # Case 1: multipart with model-A -> 500 -> fallback to model-B -> 200 {"text": "привет"}
            mode = "fail_500"
            recorded_upstream_calls.clear()

            fd1 = FormData()
            fd1.add_field("file", audio_bytes, filename="voice.ogg", content_type="audio/ogg")
            fd1.add_field("model", "model-A")
            fd1.add_field("language", "ru")

            async with session.post(url, data=fd1) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Keybroker-Model"), "model-B")
                data = await resp.json()
                self.assertEqual(data.get("text"), "привет")

            # Check upstream received exactly two calls: first for model-A, second for model-B
            self.assertEqual(len(recorded_upstream_calls), 2)
            first_call = recorded_upstream_calls[0]
            second_call = recorded_upstream_calls[1]
            self.assertEqual(first_call["model"], "model-A")
            self.assertEqual(second_call["model"], "model-B")
            # Verify input_audio fields
            self.assertEqual(first_call["input_audio"]["format"], "ogg")
            self.assertEqual(first_call["language"], "ru")
            decoded = base64.b64decode(first_call["input_audio"]["data"])
            self.assertEqual(decoded, audio_bytes)

            # Case 2: empty text on model-A also leads to fallback to model-B
            mode = "empty_text"
            recorded_upstream_calls.clear()

            fd2 = FormData()
            fd2.add_field("file", audio_bytes, filename="voice.oga", content_type="audio/ogg")
            fd2.add_field("model", "model-A")

            async with session.post(url, data=fd2) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Keybroker-Model"), "model-B")
                data = await resp.json()
                self.assertEqual(data.get("text"), "привет")
            self.assertEqual(len(recorded_upstream_calls), 2)
            # oga -> ogg conversion
            self.assertEqual(recorded_upstream_calls[0]["input_audio"]["format"], "ogg")

            # Case 3: response_format=text -> text/plain "привет"
            mode = "fail_500"
            recorded_upstream_calls.clear()

            fd3 = FormData()
            fd3.add_field("file", audio_bytes, filename="voice.ogg")
            fd3.add_field("model", "model-A")
            fd3.add_field("response_format", "text")

            async with session.post(url, data=fd3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/plain", resp.headers.get("Content-Type", ""))
                self.assertEqual(resp.headers.get("X-Keybroker-Model"), "model-B")
                raw_text = await resp.text()
                self.assertEqual(raw_text, "привет")

            # Case 4: JSON body already containing input_audio is forwarded as-is with model ladder
            mode = "fail_500"
            recorded_upstream_calls.clear()

            json_body = {
                "model": "model-A",
                "input_audio": {
                    "data": base64.b64encode(audio_bytes).decode("ascii"),
                    "format": "ogg",
                },
                "temperature": 0.2,
            }

            async with session.post(url, json=json_body) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Keybroker-Model"), "model-B")
                data = await resp.json()
                self.assertEqual(data.get("text"), "привет")
            self.assertEqual(len(recorded_upstream_calls), 2)
