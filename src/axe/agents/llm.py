"""LLM provider interface and implementations for AXE agents."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from axe.config import get_settings


class LLMResponse(BaseModel):
    """Normalized structured response from any LLM provider."""

    content: str | None = None
    parsed: dict[str, Any] | None = None
    model: str | None = None
    usage: dict[str, int] | None = None

    def get_text(self) -> str:
        return (self.content or "").strip()


class LLMProvider(ABC):
    """Abstract LLM provider supporting chat completion and structured output."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        """Return a completion for ``messages``.

        When ``response_schema`` is supplied, the provider should request
        JSON-mode/structured output and parse it into ``parsed``.
        """


class AzureFoundryProvider(LLMProvider):
    """Azure Foundry / Azure OpenAI provider using the OpenAI SDK."""

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self.endpoint = (
            endpoint or settings.azure_foundry_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        )
        self.api_key = (
            api_key or settings.azure_foundry_api_key or os.getenv("AZURE_OPENAI_API_KEY")
        )
        self.model = model or settings.azure_foundry_model
        self._client: Any | None = None

    def _client_init(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import AsyncAzureOpenAI

        if not self.endpoint or not self.api_key:
            raise RuntimeError("Azure Foundry endpoint and api_key are required")
        self._client = AsyncAzureOpenAI(
            azure_endpoint=self.endpoint,
            api_key=self.api_key,
            api_version="2024-08-01-preview",
        )
        return self._client

    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        client = self._client_init()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": response_schema.model_json_schema(),
                    "strict": True,
                },
            }
        response = await client.beta.chat.completions.parse(**kwargs)
        choice = response.choices[0]
        text = choice.message.content
        parsed: dict[str, Any] | None = None
        if response_schema is not None and getattr(choice.message, "parsed", None) is not None:
            parsed = choice.message.parsed.model_dump()
        usage = None
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return LLMResponse(content=text, parsed=parsed, model=response.model, usage=usage)


class MockProvider(LLMProvider):
    """Deterministic mock provider for tests and offline development."""

    def __init__(
        self,
        responses: list[dict[str, Any]] | None = None,
        model: str = "mock",
    ) -> None:
        self.responses = list(responses or [])
        self.model = model
        self._calls: list[tuple[list[dict[str, str]], float, type[BaseModel] | None]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self._calls.append((messages, temperature, response_schema))
        if self.responses:
            data = self.responses.pop(0)
            return LLMResponse(
                content=data.get("content"),
                parsed=data.get("parsed"),
                model=self.model,
                usage=data.get("usage"),
            )
        return LLMResponse(content="", parsed={}, model=self.model, usage={"total_tokens": 0})

    def record_call(
        self, idx: int = 0
    ) -> tuple[list[dict[str, str]], float, type[BaseModel] | None] | None:
        if idx < len(self._calls):
            return self._calls[idx]
        return None


def get_default_provider() -> LLMProvider:
    """Return a provider based on environment configuration."""
    settings = get_settings()
    if (
        settings.is_testing
        or not settings.azure_foundry_endpoint
        or not settings.azure_foundry_api_key
    ):
        return MockProvider()
    return AzureFoundryProvider()
