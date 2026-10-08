"""Multi-provider AI rewrite adapters (OCP: add a provider by registering it)."""

import re
from abc import ABC, abstractmethod
from typing import Dict, Type

import httpx

from ...domain.entities import AIConfig


class IAIProvider(ABC):
    """Every provider adapter must implement `rewrite`."""

    @abstractmethod
    async def rewrite(self, config: AIConfig, text: str) -> str:
        ...

    @abstractmethod
    async def health_check(self, config: AIConfig) -> bool:
        ...


class OpenAICompatibleProvider(IAIProvider):
    """Covers OpenAI, Groq, DeepSeek, OpenRouter, 9Router, and any
    OpenAI-compatible endpoint with a custom base_url."""

    default_base_urls: Dict[str, str] = {
        "openai": "https://api.openai.com/v1",
        "groq": "https://api.groq.com/openai/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "9router": "http://sub.legoten.com:4455/v1",
        "custom": "",
    }

    def base_url(self, config: AIConfig) -> str:
        if config.base_url:
            return config.base_url.rstrip("/")
        return self.default_base_urls.get(config.provider.value, "").rstrip("/")

    async def rewrite(self, config: AIConfig, text: str) -> str:
        url = f"{self.base_url(config)}/chat/completions"
        prompt = config.user_prompt_template.replace("{text}", text)
        payload = {
            "model": config.model,
            "temperature": config.temperature,
            "messages": [
                {"role": "system", "content": config.system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {config.api_key}"}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

    async def health_check(self, config: AIConfig) -> bool:
        try:
            await self.rewrite(config, "ping")
            return True
        except Exception:
            return False


class AnthropicProvider(IAIProvider):
    DEFAULT_URL = "https://api.anthropic.com/v1"

    async def rewrite(self, config: AIConfig, text: str) -> str:
        prompt = config.user_prompt_template.replace("{text}", text)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{(config.base_url or self.DEFAULT_URL).rstrip('/')}/messages",
                headers={
                    "x-api-key": config.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": config.model,
                    "max_tokens": 4096,
                    "temperature": config.temperature,
                    "system": config.system_prompt,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"].strip()

    async def health_check(self, config: AIConfig) -> bool:
        try:
            await self.rewrite(config, "ping")
            return True
        except Exception:
            return False


class GeminiProvider(IAIProvider):
    DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta"

    async def rewrite(self, config: AIConfig, text: str) -> str:
        prompt = config.user_prompt_template.replace("{text}", text)
        base = (config.base_url or self.DEFAULT_URL).rstrip("/")
        url = f"{base}/models/{config.model}:generateContent?key={config.api_key}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                json={
                    "systemInstruction": {"parts": [{"text": config.system_prompt}]},
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": config.temperature},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    async def health_check(self, config: AIConfig) -> bool:
        try:
            await self.rewrite(config, "ping")
            return True
        except Exception:
            return False


class AIProviderFactory:
    """Registry-based factory — register new providers without modifying callers."""

    def __init__(self) -> None:
        self._registry: Dict[str, Type[IAIProvider]] = {
            "openai": OpenAICompatibleProvider,
            "groq": OpenAICompatibleProvider,
            "deepseek": OpenAICompatibleProvider,
            "openrouter": OpenAICompatibleProvider,
            "9router": OpenAICompatibleProvider,
            "custom": OpenAICompatibleProvider,
            "anthropic": AnthropicProvider,
            "gemini": GeminiProvider,
        }

    def register(self, name: str, provider_cls: Type[IAIProvider]) -> None:
        self._registry[name] = provider_cls

    def get(self, config: AIConfig) -> IAIProvider:
        key = config.provider.value if hasattr(config.provider, "value") else str(config.provider)
        cls = self._registry.get(key)
        if cls is None:
            raise ValueError(f"Unknown AI provider: {key}")
        return cls()
