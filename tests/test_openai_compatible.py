import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from src.backends.llm import _lm_studio_json, _openai_compatible_json, generate_knowledge
from src.backends.openai_compatible import api_key_from_env, chat_completion
from src.visual.vlm import OpenAICompatibleVLMBackend


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        size = int(self.headers["Content-Length"])
        self.__class__.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(size))})
        body = self.__class__.requests[-1]["body"]
        if body["model"] == "knowledge-model":
            content = json.dumps({"one_sentence_conclusion": {"content": "summary", "evidence_segment_ids": ["seg_000001"]}, "knowledge_points": [], "keywords": []})
        else:
            content = json.dumps({"status": "resolved", "answer": "visual answer", "confidence": 0.9, "reason": ""})
        response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(response))); self.end_headers(); self.wfile.write(response)

    def log_message(self, *_args):
        return None


class OpenAICompatibleTests(unittest.TestCase):
    def setUp(self):
        _Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self):
        self.server.shutdown(); self.thread.join()
        os.environ.pop("VIDEO_KNOWLEDGE_TEST_KEY", None)

    def test_uses_env_key_without_putting_it_in_settings(self):
        os.environ["VIDEO_KNOWLEDGE_TEST_KEY"] = "test"
        settings = {"base_url": self.base_url, "model": "mock-model", "api_key_env": "VIDEO_KNOWLEDGE_TEST_KEY"}
        chat_completion(settings, [{"role": "user", "content": "hello"}], response_format=None)
        self.assertEqual(_Handler.requests[0]["authorization"], "Bearer test")
        self.assertNotIn("test", json.dumps(settings))

    def test_missing_env_key_fails_without_a_request(self):
        with self.assertRaisesRegex(RuntimeError, "VIDEO_KNOWLEDGE_TEST_KEY"):
            api_key_from_env({"api_key_env": "VIDEO_KNOWLEDGE_TEST_KEY"})
        self.assertEqual(_Handler.requests, [])

    def test_knowledge_json_schema_uses_generic_endpoint(self):
        settings = {"base_url": self.base_url, "model": "mock-model", "api_key_env": None}
        schema = {"name": "test", "schema": {"type": "object"}}
        response = _openai_compatible_json("prompt", settings, schema)
        self.assertEqual(response["status"], "resolved")
        self.assertEqual(_Handler.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(_Handler.requests[0]["body"]["response_format"]["type"], "json_schema")

    def test_legacy_lm_studio_settings_remain_compatible(self):
        schema = {"name": "test", "schema": {"type": "object"}}
        response = _lm_studio_json("prompt", {"base_url": self.base_url, "model": "local-model"}, schema)
        self.assertEqual(response["status"], "resolved")
        self.assertIsNone(_Handler.requests[0]["authorization"])

    def test_knowledge_backend_accepts_flat_openai_compatible_config(self):
        config = {"backend": "openai_compatible", "base_url": self.base_url, "model": "knowledge-model", "api_key_env": None}
        generated, provenance = generate_knowledge({"video_id": "test"}, [{"start": 0, "end": 1, "text": "source"}], config)
        self.assertEqual(generated["one_sentence_conclusion"]["content"], "summary")
        self.assertEqual(provenance["backend"], "openai_compatible")

    def test_vlm_generic_endpoint_sends_image_without_local_lifecycle(self):
        with TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"; image.write_bytes(b"not-a-real-image")
            backend = OpenAICompatibleVLMBackend({"base_url": self.base_url, "model": "mock-vlm", "api_key_env": None})
            answer, _ = backend.answer({"requested_information": ["relation"]}, [{"frame_id": "frame_1", "path": str(image)}], {"text": "", "confidence": 0}, "look here")
        self.assertEqual(answer["status"], "resolved")
        self.assertEqual(backend.unload()["lifecycle"], "external_api_no_local_model")
        content = _Handler.requests[0]["body"]["messages"][0]["content"]
        self.assertEqual(content[1]["type"], "image_url")


if __name__ == "__main__":
    unittest.main()
