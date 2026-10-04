import json
import unittest

import httpx

from llm_gateway.adapters.base import AdapterChatRequest, AdapterMessage
from llm_gateway.adapters.openai_compatible import OpenAICompatibleAdapter
from llm_gateway.core.errors import UpstreamProtocolError, UpstreamUnavailableError
from llm_gateway.registry.models import ModelDeployment


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = OpenAICompatibleAdapter(
            ModelDeployment(
                id="summary@lm-studio",
                logical_model="summary",
                adapter="openai",
                endpoint="http://studio:1234/v1",
                upstream_model="qwen3-4b",
            )
        )
        self.request = AdapterChatRequest(
            model="qwen3-4b",
            messages=[AdapterMessage("user", "요약해줘")],
        )

    async def asyncTearDown(self):
        await self.adapter.aclose()

    async def bind(self, handler):
        client = await self.adapter._ensure_client()
        client._transport = httpx.MockTransport(handler)

    async def test_chat_path_payload_and_usage(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "qwen3-4b")
            self.assertFalse(body["stream"])
            return httpx.Response(
                200,
                json={
                    "model": "qwen3-4b",
                    "choices": [{"message": {"content": "요약"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 2},
                },
            )

        await self.bind(handler)
        response = await self.adapter.chat(self.request)
        self.assertEqual(response.content, "요약")
        self.assertEqual(response.usage.input_tokens, 12)
        self.assertEqual(response.usage.output_tokens, 2)
        self.assertIsNone(response.timings.generation_sec)

    async def test_stream_waits_for_usage_after_finish(self):
        events = [
            {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "요약"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2}},
        ]

        def handler(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(json.loads(request.content)["stream_options"], {"include_usage": True})
            body = (
                ": heartbeat\n\n"
                + "".join("data: " + json.dumps(event) + "\n\n" for event in events)
                + "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=body)

        await self.bind(handler)
        chunks = [chunk async for chunk in self.adapter.stream_chat(self.request)]
        self.assertEqual([c.delta for c in chunks], ["요약", ""])
        self.assertEqual(chunks[-1].finish_reason, "stop")
        self.assertEqual(chunks[-1].usage.output_tokens, 2)

    async def test_truncated_and_invalid_streams_fail(self):
        for body in ("data: not-json\n\n", 'data: {"choices": []}\n\n'):
            with self.subTest(body=body):
                await self.adapter.aclose()
                await self.bind(lambda request, body=body: httpx.Response(200, text=body))
                with self.assertRaises(UpstreamProtocolError):
                    _ = [c async for c in self.adapter.stream_chat(self.request)]

    async def test_health_requires_configured_model(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/models")
            return httpx.Response(200, json={"data": [{"id": "other-model"}]})

        await self.bind(handler)
        self.assertFalse(await self.adapter.health())
        self.adapter._client._transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": "qwen3-4b"}]}),
        )
        self.assertTrue(await self.adapter.health())

    async def test_transport_errors_are_translated(self):
        def handler(request):
            raise httpx.ConnectError("unreachable", request=request)

        await self.bind(handler)
        with self.assertRaises(UpstreamUnavailableError):
            await self.adapter.chat(self.request)

    async def test_missing_usage_and_structured_output(self):
        self.request.json_schema = {"type": "object"}
        payload = self.adapter._build_payload(self.request, stream=False)
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], {"type": "object"})
        await self.bind(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                },
            )
        )
        response = await self.adapter.chat(self.request)
        self.assertIsNone(response.usage.input_tokens)
        self.assertIsNone(response.usage.output_tokens)


if __name__ == "__main__":
    unittest.main()
