"""Tiny OpenAI-compatible chat client (stdlib only).

Works with OpenAI, OpenRouter, Groq, Together, Gemini's compat endpoint,
LM Studio, Ollama, or anything else that speaks /chat/completions.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


API_ROOT = ""

# Some providers (Groq, Cloudflare-fronted APIs) reject Python's default
# urllib User-Agent with a 403. Always identify ourselves properly.
USER_AGENT = "DeadlineWatch/1.0 (+https://github.com/local/deadlinewatch)"


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        too_large: bool = False,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.too_large = too_large


# "try again in 1.365s" - Groq tells us how long to wait
_RETRY_AFTER = re.compile(r"try again in\s+([\d.]+)\s*s", re.I)


class TokenBucket:
    """Keeps us under a provider's tokens-per-minute limit.

    Free tiers (Groq: 8k TPM) will 429 a tool that reads 40 emails at once.
    Tracking real usage and pausing keeps the pipeline running instead of
    losing whole batches to rate limits.
    """

    def __init__(self, limit_per_minute: int):
        self.limit = limit_per_minute
        self.events: list[tuple[float, int]] = []

    def _prune(self) -> None:
        cutoff = time.time() - 60
        self.events = [event for event in self.events if event[0] > cutoff]

    def before_call(self, estimate: int) -> int:
        """Reserve room for a request. Returns the slot to settle later."""
        if not self.limit:
            return -1
        self._prune()
        used = sum(tokens for _, tokens in self.events)
        if used + estimate >= self.limit and self.events:
            wait = 60 - (time.time() - self.events[0][0]) + 0.3
            if wait > 0:
                time.sleep(min(wait, 65))
            self._prune()
        self.events.append((time.time(), estimate))
        return len(self.events) - 1

    def settle(self, slot: int, tokens: int) -> None:
        """Replace the estimate with what the provider actually charged."""
        if slot < 0 or slot >= len(self.events):
            return
        if tokens:
            self.events[slot] = (self.events[slot][0], int(tokens))
        self._prune()


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model reply (handles ``` fences and prose)."""
    if not text:
        raise LLMError("empty model response")
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model reply was not valid JSON: {cleaned[:300]}") from exc
    raise LLMError(f"model reply was not valid JSON: {cleaned[:300]}")


def _content_to_text(content) -> str:
    """Some providers return content as a list of parts instead of a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content or "")


class LLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 90,
        temperature: float = 0.0,
        tpm_limit: int = 6000,
        max_tokens: int = 1200,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._supports_json_mode: bool | None = None
        self.bucket = TokenBucket(tpm_limit)

    # -- transport -------------------------------------------------------
    def _post(self, payload: dict, slot: int = -1) -> dict:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")[:600]
            if exc.code == 413:
                raise LLMError(
                    "the request is larger than this provider allows (413): "
                    + body_text[:200],
                    too_large=True,
                ) from exc
            if exc.code == 429:
                match = _RETRY_AFTER.search(body_text)
                wait = float(match.group(1)) if match else 30.0
                raise LLMError(
                    f"rate limited by the provider (429): {body_text[:200]}",
                    retry_after=wait,
                ) from exc
            if exc.code == 404 and "model" in body_text.lower():
                raise LLMError(
                    f"model '{self.model}' is not available on {self.base_url}.\n"
                    "  Check the model name, or list what your provider offers:\n"
                    f"  GET {self.base_url}/models  (with the same Authorization header)"
                ) from exc
            if exc.code == 401:
                raise LLMError(
                    "the LLM rejected the API key (401). Check LLM_API_KEY in .env."
                ) from exc
            raise LLMError(f"HTTP {exc.code} from LLM endpoint: {body_text}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"cannot reach LLM endpoint {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError("LLM request timed out") from exc

        usage = body.get("usage") or {}
        self.bucket.settle(slot, int(usage.get("total_tokens") or 0))
        return body

    def chat_json(self, system: str, user: str, retries: int = 4) -> dict:
        """Send a chat request and return the parsed JSON object."""
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self._supports_json_mode is not False:
                payload["response_format"] = {"type": "json_object"}

            try:
                # Rough estimate of the request size so we do not burst past
                # the provider's per-minute token budget.
                estimate = (len(system) + len(user)) // 3
                slot = self.bucket.before_call(estimate)
                raw = self._post(payload, slot)
            except LLMError as exc:
                last_error = exc
                message = str(exc)
                # Provider rejected response_format -> drop it and retry.
                if "response_format" in message and self._supports_json_mode is None:
                    self._supports_json_mode = False
                    continue
                # Rate limited -> wait exactly as long as the provider asked.
                if exc.retry_after is not None:
                    if attempt < retries:
                        time.sleep(min(exc.retry_after + 0.5, 70))
                        continue
                    raise
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise

            self._supports_json_mode = True
            try:
                content = raw["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"unexpected LLM response shape: {str(raw)[:300]}") from exc
            return _extract_json(_content_to_text(content))

        raise LLMError(f"LLM call failed: {last_error}")
