"""Выбор провайдера LLM по переменной PROVIDER из .env."""
import os

from .base import END, MAX_TOKENS, TOOL_USE, USAGE_KEYS, LLMClient, LLMResponse, ToolCall

DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "qwen3.5:9b"}


def provider_name() -> str:
    return os.getenv("PROVIDER", "anthropic").strip().lower()


def model_name() -> str:
    return os.getenv("MODEL") or DEFAULT_MODELS.get(provider_name(), "")


def make_client(system: str, tools: list[dict], max_tokens: int = 4096) -> LLMClient:
    """tools — схемы в формате Anthropic (как в tools.py); адаптер переводит их сам."""
    provider, model = provider_name(), model_name()
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, system, tools, max_tokens)
    if provider == "openai":
        from .openai_client import OpenAIClient
        return OpenAIClient(model, system, tools, max_tokens)
    raise ValueError(f"Неизвестный PROVIDER={provider!r}: ожидается anthropic или openai")


__all__ = ["END", "MAX_TOKENS", "TOOL_USE", "USAGE_KEYS", "LLMClient", "LLMResponse",
           "ToolCall", "make_client", "model_name", "provider_name"]
