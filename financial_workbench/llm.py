import asyncio
import os

import httpx

from .models import Extraction


class ProviderError(RuntimeError):
    pass


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
        "max_completion_tokens": 800 if structured else 3000,
    }
    if structured:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "financial_facts",
                "strict": True,
                "schema": Extraction.model_json_schema(),
            },
        }
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(5):
            try:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=body,
                )
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise ProviderError(
                        "The model provider could not be reached. Retry the job."
                    ) from exc
                await asyncio.sleep(2**attempt)
                continue
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < 4:
                try:
                    delay = min(
                        30,
                        max(1, float(response.headers.get("retry-after", 2**attempt))),
                    )
                except ValueError:
                    delay = 2**attempt
                await asyncio.sleep(delay)
                continue
            if response.is_error:
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
                raise ProviderError(
                    "Model output was incomplete or refused. No partial extraction was accepted."
                )
            return choice["message"]["content"]
    raise ProviderError(
        "Model provider rate limit or service unavailable. Retry later."
    )
