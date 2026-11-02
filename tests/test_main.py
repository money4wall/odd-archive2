import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("odd_archive_main", Path(__file__).parents[1] / "main.py")
main = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(main)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()


class PipelineTests(unittest.TestCase):
    def test_json_parser_handles_fences_and_surrounding_text(self):
        self.assertEqual(main.parse_json_object('```json\n{"x": 1}\n```'), {"x": 1})
        self.assertEqual(main.parse_json_object('prefix {"y": 2} suffix'), {"y": 2})

    def test_rate_limit_delay_uses_server_message(self):
        delay = main.server_retry_seconds({}, "Please try again in 8.3025s", 0)
        self.assertAlmostEqual(delay, 9.8025)

    def test_groq_request_uses_gpt_oss_json_compatible_settings(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured.update(json.loads(request.data.decode()))
            return FakeResponse()

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), \
             patch.object(main.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(main.time, "sleep"), \
             patch.object(main, "_last_request_at", 0):
            self.assertEqual(main.groq_json("Return JSON", 100), {"ok": True})

        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertIs(captured["include_reasoning"], False)
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertNotIn("reasoning_format", captured)

    def test_http_429_is_retried(self):
        calls = {"count": 0}

        def fake_urlopen(request, timeout=0):
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.HTTPError(request.full_url, 429, "rate limit", {}, None)
            return FakeResponse()

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), \
             patch.object(main.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(main.time, "sleep"), \
             patch.object(main, "_last_request_at", 0):
            self.assertEqual(main.groq_json("Return JSON", 100), {"ok": True})
        self.assertEqual(calls["count"], 2)

    def test_completed_scene_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_work = main.WORK
            main.WORK = Path(tmp) / "work"
            try:
                topic = "resume-test"
                cache = main.topic_cache(topic)
                brief = {"heading": "Test", "summary": "Test", "search_query": "test"}
                saved = {
                    "heading": "Test",
                    "search_query": "test",
                    "narration": "A previously generated narration. " * 20,
                }
                main.atomic_json(cache / "scenes" / "scene_01.json", saved)
                with patch.object(main, "groq_json", side_effect=AssertionError("API should not be called")):
                    result = main.generate_scene(topic, brief, 1, cache)
                self.assertEqual(result, saved)
            finally:
                main.WORK = old_work

    def test_pixabay_skips_empty_large_url(self):
        hits = [{"videos": {"large": {"url": "", "width": 3840, "height": 2160},
                            "medium": {"url": "http://x/medium.mp4", "width": 1920, "height": 1080}}}]
        self.assertEqual(main.pick_video(hits)["url"], "http://x/medium.mp4")
        self.assertIsNone(main.pick_video([{"videos": {"large": {"url": ""}}}]))

    def test_pixabay_prefers_1080p_and_keeps_relevance_order_on_ties(self):
        hits = [
            {"videos": {"large": {"url": "http://x/first.mp4", "width": 1920, "height": 1080}}},
            {"videos": {"large": {"url": "http://x/4k.mp4", "width": 3840, "height": 2160}}},
        ]
        self.assertEqual(main.pick_video(hits)["url"], "http://x/first.mp4")

    def test_truncated_response_raises_token_budget(self):
        budgets = []
        responses = [
            {"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]},
        ]

        class R(FakeResponse):
            def __init__(self, body): self.body = body
            def read(self): return json.dumps(self.body).encode()

        def fake_urlopen(request, timeout=0):
            budgets.append(json.loads(request.data.decode())["max_completion_tokens"])
            return R(responses[len(budgets) - 1])

        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
             patch.object(main.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(main.time, "sleep"), patch.object(main, "_last_request_at", 0):
            self.assertEqual(main.groq_json("x", 1000), {"ok": True})
        self.assertEqual(budgets, [1000, 1500])

    def test_topic_rotation(self):
        self.assertEqual(main.pick_topic("  My topic "), "My topic")
        with patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "1"}):
            first = main.pick_topic("")
        with patch.dict(os.environ, {"GITHUB_RUN_NUMBER": "2"}):
            second = main.pick_topic("")
        self.assertNotEqual(first, second)


    def test_json_validate_failed_400_is_retried(self):
        calls = {"count": 0}
        body = b'{"error":{"code":"json_validate_failed","message":"Failed to generate JSON"}}'

        def fake_urlopen(request, timeout=0):
            calls["count"] += 1
            if calls["count"] == 1:
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(body))
            return FakeResponse()

        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
             patch.object(main.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(main.time, "sleep"), patch.object(main, "_last_request_at", 0):
            self.assertEqual(main.groq_json("x", 100), {"ok": True})
        self.assertEqual(calls["count"], 2)

    def test_other_400_still_fails(self):
        def fake_urlopen(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b'{"error":{"code":"invalid_api_key"}}'))

        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}), \
             patch.object(main.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(main.time, "sleep"), patch.object(main, "_last_request_at", 0):
            with self.assertRaises(RuntimeError):
                main.groq_json("x", 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
