"""Tests for the LLM provider interface and mock provider."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from axe.agents.llm import AzureFoundryProvider, MockProvider


class _DummySchema(BaseModel):
    answer: str


@pytest.mark.asyncio
async def test_mock_provider_returns_queued_response() -> None:
    provider = MockProvider(responses=[{"parsed": {"answer": "yes"}}])
    response = await provider.complete([{"role": "user", "content": "hello"}])
    assert response.parsed == {"answer": "yes"}
    assert response.model == "mock"


@pytest.mark.asyncio
async def test_mock_provider_records_calls() -> None:
    provider = MockProvider(responses=[{"content": "ok"}])
    await provider.complete([{"role": "user", "content": "ping"}], temperature=0.5)
    call = provider.record_call(0)
    assert call is not None
    assert call[0] == [{"role": "user", "content": "ping"}]
    assert call[1] == 0.5


@pytest.mark.asyncio
async def test_mock_provider_schema_passed() -> None:
    provider = MockProvider()
    await provider.complete(
        [{"role": "user", "content": "q"}],
        response_schema=_DummySchema,
    )
    call = provider.record_call(0)
    assert call is not None
    assert call[2] is _DummySchema


def test_azure_provider_reads_credentials() -> None:
    provider = AzureFoundryProvider(
        endpoint="https://x.openai.azure.com", api_key="my-key", model="gpt-4o"
    )
    assert provider.api_key == "my-key"
    assert provider.endpoint == "https://x.openai.azure.com"
    assert provider.model == "gpt-4o"
