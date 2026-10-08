"""Multi-provider AI rewrite adapters (OCP: add a provider by registering it).

2026 refresh: current model families, 9Router/OpenRouter extras, timeouts,
retry with back-off, and conservative error mapping so the pipeline can fall
back to the original text instead of losing the message.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, Type

import httpx

from ...domain.entities import AIConfig

logger = logging.getLogger(__name__)

#: default network budget per rewrite attempt
REQUEST_TIMEOUT = 45.0
#: transient-error retries (429 / 5xx / timeouts)
MAX_RETRIES = 2

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class IAIProvider(ABC):
    """Every provider adapter must implement `rewrite`."""

    @abstractmethod
    async def rewrite(self, config: AIConfig, text: str) -> str:
        ...

    @abstractmethod
    async def health_check(self, config: AIConfig) -> bool:
        ...


async def _post_json(url: str, *, headers: dict, payload: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST JSON with retry on transient failures. Raises on final failure."""
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    wait = min(2 ** attempt, 8)
                    logger.debug("AI %d → retry in %ds (attempt %d)", resp.status_code, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                raise
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                raise
    assert last_exc is not None
    raise last_exc


class OpenAICompatibleProvider(IAIProvider):
    """Covers OpenAI, Groq, DeepSeek, OpenRouter, 9Router, xAI, and any
    OpenAI-compatible endpoint with a custom base_url."""

    default_base_urls: Dict[str, str] = {
        "openai": "https://api.openai.com/v1",
        "groq": "https://api.groq.com/openai/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "9router": "http://sub.legoten.com:4455/v1",
        "ninerouter": "http://sub.legoten.com:4455/v1",
        "xai": "https://api.x.ai/v1",
        "mistral": "https://api.mistral.ai/v1",
        "together": "https://api.together.xyz/v1",
        "fireworks": "https://api.fireworks.ai/inference/v1",
        "custom": "",
    }

    def base_url(self, config: AIConfig) -> str:
        if config.base_url:
            return config.base_url.rstrip("/")
        key = config.provider.value if hasattr(config.provider, "value") else str(config.provider)
        return self.default_base_urls.get(key, "").rstrip("/")

    def headers(self, config: AIConfig) -> dict:
        headers = {"Authorization": f"Bearer {config.api_key}"}
        key = config.provider.value if hasattr(config.provider, "value") else str(config.provider)
        if key == "openrouter":
            # OpenRouter attribution headers (optional but recommended).
            headers["HTTP-Referer"] = "https://github.com/Opselon/AutoTelegramForward"
            headers["X-Title"] = "AutoTelegramForward"
        return headers

    async def rewrite(self, config: AIConfig, text: str) -> str:
        base = self.base_url(config)
        if not base:
            raise ValueError("custom provider requires base_url to be set")
        url = f"{base}/chat/completions"
        prompt = config.user_prompt_template.replace("{text}", text)
        payload = {
            "model": config.model,
            "temperature": config.temperature,
            "messages": [
                {"role": "system", "content": config.system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        data = await _post_json(url, headers=self.headers(config), payload=payload)
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise ValueError(f"unexpected chat-completions response: {exc}")

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
        url = f"{(config.base_url or self.DEFAULT_URL).rstrip('/')}/messages"
        data = await _post_json(
            url,
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": "2023-06-01",
            },
            payload={
                "model": config.model,
                "max_tokens": 4096,
                "temperature": config.temperature,
                "system": config.system_prompt,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        try:
            return data["content"][0]["text"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise ValueError(f"unexpected Anthropic response: {exc}")

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
        data = await _post_json(
            url,
            headers={},
            payload={
                "systemInstruction": {"parts": [{"text": config.system_prompt}]},
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": config.temperature},
            },
        )
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise ValueError(f"unexpected Gemini response: {exc}")

    async def health_check(self, config: AIConfig) -> bool:
        try:
            await self.rewrite(config, "ping")
            return True
        except Exception:
            return False


class AIProviderFactory:
    """Registry-based factory — register new providers without modifying callers."""

    #: current model families per provider (shown in the bot UI picker).
    MODELS: Dict[str, list] = {
        "openai": ["gpt-5-mini", "gpt-5", "gpt-4.1-mini", "gpt-4o-mini"],
        "anthropic": ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"],
        "gemini": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
        "groq": ["llama-3.3-70b-versatile", "moonshotai/kimi-k2-instruct"],
        "deepseek": ["deepseek-chat", "deepseek-reasoner"],
        "openrouter": ["openai/gpt-5-mini", "anthropic/claude-sonnet-4-5", "google/gemini-2.5-flash"],
        "9router": ["auto"],
        "ninerouter": ["auto"],
        "xai": ["grok-4", "grok-3-mini"],
        "mistral": ["mistral-large-latest", "mistral-small-latest"],
        "together": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        "fireworks": ["accounts/fireworks/models/llama-v3p3-70b-instruct"],
        "custom": ["custom-model"],
    }

    def __init__(self) -> None:
        self._registry: Dict[str, Type[IAIProvider]] = {
            "openai": OpenAICompatibleProvider,
            "groq": OpenAICompatibleProvider,
            "deepseek": OpenAICompatibleProvider,
            "openrouter": OpenAICompatibleProvider,
            "9router": OpenAICompatibleProvider,
            "ninerouter": OpenAICompatibleProvider,
            "xai": OpenAICompatibleProvider,
            "mistral": OpenAICompatibleProvider,
            "together": OpenAICompatibleProvider,
            "fireworks": OpenAICompatibleProvider,
            "custom": OpenAICompatibleProvider,
            "anthropic": AnthropicProvider,
            "gemini": GeminiProvider,
        }

    def register(self, name: str, provider_cls: Type[IAIProvider]) -> None:
        self._registry[name] = provider_cls

    def providers(self) -> list:
        return sorted(self._registry)

    def models_for(self, provider: str) -> list:
        return self.MODELS.get(provider, ["default"])

    def get(self, config: AIConfig) -> IAIProvider:
        key = config.provider.value if hasattr(config.provider, "value") else str(config.provider)
        cls = self._registry.get(key)
        if cls is None:
            raise ValueError(f"Unknown AI provider: {key}")
        return cls()
