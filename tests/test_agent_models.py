import unittest
from types import SimpleNamespace

from app.services.agent_models import AgentModelGatewayClient, AgentModelRegistry


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def complete_async(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def stream(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        for token in self.result or ():
            yield token


class PartialFailureClient(FakeClient):
    async def stream(self, messages):
        self.calls += 1
        yield "partial"
        raise RuntimeError("stream interrupted")


class AgentModelRegistryTests(unittest.TestCase):
    def test_agent_can_override_model_without_changing_global_default(self):
        settings = SimpleNamespace(
            ai_provider="ollama",
            ollama_model="default-model",
            openai_model="default-openai",
            ai_temperature=0.35,
            ai_max_tokens=512,
            agent_model_understanding_model="small-understanding-model",
            agent_model_understanding_provider="ollama",
            agent_model_safety_model="safety-model",
            agent_model_safety_provider="ollama",
        )

        registry = AgentModelRegistry(settings)

        self.assertEqual(registry.profile_for("UnderstandingAgent").model, "small-understanding-model")
        self.assertEqual(registry.profile_for("ResponseAgent").model, "default-model")


class AgentModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_falls_back_and_opens_circuit(self):
        primary = FakeClient(error=RuntimeError("provider down"))
        fallback = FakeClient(result="CONSULT")
        client = AgentModelGatewayClient(
            primary,
            fallback,
            key="test-gateway",
            failure_threshold=1,
            reset_seconds=60,
        )

        self.assertEqual(await client.complete_async([]), "CONSULT")
        self.assertEqual(await client.complete_async([]), "CONSULT")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 2)

    async def test_stream_falls_back_only_before_first_token(self):
        before_token_failure = FakeClient(error=RuntimeError("provider down"))
        fallback = FakeClient(result=["safe", " response"])
        client = AgentModelGatewayClient(
            before_token_failure,
            fallback,
            key="stream-before-token",
            failure_threshold=1,
            reset_seconds=60,
        )
        tokens = [token async for token in client.stream([])]
        self.assertEqual(tokens, ["safe", " response"])

        partial = PartialFailureClient()
        unused_fallback = FakeClient(result=["must-not-append"])
        client = AgentModelGatewayClient(
            partial,
            unused_fallback,
            key="stream-after-token",
            failure_threshold=1,
            reset_seconds=60,
        )
        seen = []
        with self.assertRaisesRegex(RuntimeError, "stream interrupted"):
            async for token in client.stream([]):
                seen.append(token)
        self.assertEqual(seen, ["partial"])
        self.assertEqual(unused_fallback.calls, 0)


if __name__ == "__main__":
    unittest.main()
