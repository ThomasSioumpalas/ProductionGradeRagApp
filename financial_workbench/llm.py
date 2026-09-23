import asyncio
import os
import re

import httpx

from .models import Extraction


class ProviderError(RuntimeError):
    pass


class IncompleteOutputError(ProviderError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


MAX_PROVIDER_ATTEMPTS = 8


def retry_delay(response, attempt):
    """Honor Groq's retry window, including longer daily-token resets."""
    header = response.headers.get("retry-after")
    if header:
        try:
            # Provider windows can be approximate; leave a small margin so a
            # retry does not arrive just before the reported quota clears.
            return min(86_400, max(1, float(header)) + 1)
        except ValueError:
            pass

    try:
        message = response.json().get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    match = re.search(
        r"try again in\s+((?:\d+(?:\.\d+)?\s*[hms]\s*)+)", message, re.I
    )
    if match:
        parts = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", match.group(1), re.I)
        seconds = sum(
            float(value) * {"h": 3600, "m": 60, "s": 1}[unit.lower()]
            for value, unit in parts
        )
        if seconds:
            return min(86_400, max(1, seconds) + 1)
    return min(30, 2**attempt)


async def completion(messages, structured=False):
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise ProviderError(
            "Set GROQ_API_KEY on the backend before extracting or asking questions."
        )
    body = {
        "model": os.getenv("GROQ_EXTRACTION_MODEL", "openai/gpt-oss-120b"),
        "messages": messages,
        "temperature": 0,
        # Primary statement pages need a bounded list of figures, not a long
        # report. Keeping this small avoids multi-minute generation stalls.
        "max_completion_tokens": 1600 if structured else 3000,
    }
    if structured:
        # GPT-OSS defaults to medium reasoning, which can consume the entire
        # completion budget before producing the JSON payload. Extraction is a
        # constrained mapping task, so low effort is both sufficient and safer
        # for Groq's small on-demand TPM allowance.
        body["reasoning_effort"] = "low"
        body["include_reasoning"] = False
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "financial_facts",
                "strict": True,
                "schema": Extraction.model_json_schema(),
            },
        }
    used_json_mode_fallback = False
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(MAX_PROVIDER_ATTEMPTS):
            try:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=body,
                )
            except httpx.TransportError as exc:
                if attempt == MAX_PROVIDER_ATTEMPTS - 1:
                    raise ProviderError(
                        "The model provider could not be reached. Retry the job."
                    ) from exc
                await asyncio.sleep(2**attempt)
                continue
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < MAX_PROVIDER_ATTEMPTS - 1:
                delay = retry_delay(response, attempt)
                await asyncio.sleep(delay)
                continue
            if response.is_error:
                # Some otherwise-supported open-weight models occasionally fail
                # Groq's server-side generation validator. Retry without any
                # provider response format; Extraction.model_validate_json still
                # performs the complete local schema validation before facts are
                # used, so malformed output is never accepted.
                if (
                    structured
                    and response.status_code == 400
                    and "json_validate_failed" in response.text
                    and not used_json_mode_fallback
                ):
                    body.pop("response_format", None)
                    used_json_mode_fallback = True
                    continue
                # Return the provider's compact diagnostic. It distinguishes a
                # request-size limit from key, model-access, or quota problems.
                detail = response.text.replace("\n", " ").strip()[:500]
                raise ProviderError(
                    f"Model provider returned HTTP {response.status_code}: {detail or 'no detail returned'}"
                )
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") != "stop" or not choice["message"].get(
                "content"
            ):
                payload = response.json()
                usage = payload.get("usage", {})
                reason = choice.get("finish_reason", "unknown")
                refusal = choice.get("message", {}).get("refusal")
                raise IncompleteOutputError(
                    reason,
                    "Model output was incomplete or refused: "
                    f"finish_reason={reason}, "
                    f"completion_tokens={usage.get('completion_tokens', 'unknown')}, "
                    f"refusal={str(refusal)[:160] if refusal else 'none'}. "
                    "No partial extraction was accepted."
                )
            return choice["message"]["content"]
    raise ProviderError(
        "Model provider rate limit or service unavailable. Retry later."
    )
