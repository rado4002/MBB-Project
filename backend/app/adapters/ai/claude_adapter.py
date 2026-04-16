"""
Claude AI adapter.

Wraps the Anthropic Messages API with:
  - Circuit breaker (3 consecutive failures → open)
  - Exponential back-off retry (max 3 attempts)
  - Timeout from settings (default 25 s)
  - DRC tone system prompt injection
"""
from __future__ import annotations

import time

import anthropic
import structlog

from app.adapters.base import BaseAIAdapter
from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

_MAX_RETRIES = 3
_RETRY_DELAYS = (2.0, 5.0, 10.0)

# DRC tone system prompt appended to every generate() call
_DRC_SYSTEM_SUFFIX = (
    "\n\nTONE RULES (mandatory):\n"
    "- Feel like a helpful young Congolese friend — warm, casual, respectful.\n"
    "- 2-3 sentences MAX per message.\n"
    "- Help first, sell second.\n"
    "- NEVER robotic, NEVER pushy, NEVER formal.\n"
    "- Respect opt-out signals instantly ('stop', 'arrête', 'yaka te', 'tika').\n"
    "- Use the customer's preferred language (Lingala / French / Swahili)."
)


class AIAdapterError(RuntimeError):
    """Raised when the AI model is unreachable after retries."""


class ClaudeAdapter(BaseAIAdapter):
    """Anthropic Claude Messages API adapter."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.claude_api_key,
            timeout=settings.claude_timeout_s,
            max_retries=0,   # we handle retries ourselves
        )
        self._failures = 0
        self._circuit_open = False

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _record_success(self) -> None:
        self._failures = 0
        self._circuit_open = False

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _MAX_RETRIES:
            self._circuit_open = True
            log.error("claude.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if self._circuit_open:
            raise AIAdapterError("Claude circuit breaker open — too many consecutive failures")

    # ── BaseAIAdapter interface ───────────────────────────────────────────────

    async def generate(self, prompt: str, system: str, max_tokens: int) -> str:
        """
        Generate a completion via the Anthropic Messages API.

        Args:
            prompt:     User-side message content.
            system:     System prompt (DRC tone suffix is appended automatically).
            max_tokens: Maximum tokens in the response.

        Returns the assistant's text response.
        """
        import asyncio

        self._check_circuit()
        full_system = system + _DRC_SYSTEM_SUFFIX

        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                t0 = time.monotonic()
                message = await self._client.messages.create(
                    model=settings.claude_model,
                    max_tokens=max_tokens,
                    system=full_system,
                    messages=[{"role": "user", "content": prompt}],
                )
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                self._record_success()
                text = message.content[0].text if message.content else ""
                log.info(
                    "claude.generated",
                    model=settings.claude_model,
                    input_tokens=message.usage.input_tokens,
                    output_tokens=message.usage.output_tokens,
                    elapsed_ms=elapsed_ms,
                )
                return text
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                log.warning("claude.retry", attempt=attempt, error=str(exc))
                self._record_failure()
                if delay is None:
                    raise AIAdapterError(f"Claude unreachable after {attempt} attempts") from exc
                await asyncio.sleep(delay)
            except anthropic.APIStatusError as exc:
                # 4xx errors shouldn't be retried
                log.error("claude.api_error", status=exc.status_code, error=str(exc))
                raise AIAdapterError(f"Claude API error {exc.status_code}: {exc.message}") from exc

        return ""  # unreachable

    async def detect_language(self, text: str) -> str:
        """
        Use Claude to detect language when keyword fast-path is inconclusive.
        Returns: 'lingala' | 'french' | 'swahili'
        """
        prompt = (
            f"Detect the primary language of this text. "
            f"Respond with exactly one word: lingala, french, or swahili.\n\nText: {text[:200]}"
        )
        try:
            result = await self.generate(
                prompt=prompt,
                system="You are a language detector for Congolese languages.",
                max_tokens=10,
            )
            lang = result.strip().lower()
            if lang in ("lingala", "french", "swahili"):
                return lang
        except AIAdapterError:
            pass
        return "french"  # Safe default
