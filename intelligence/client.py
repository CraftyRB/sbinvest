"""Cerebras client (OpenAI-compatible SDK).

Endpoint: https://api.cerebras.ai/v1
Model:    gpt-oss-120b (from env.txt)

Primary path uses strict JSON-Schema constrained decoding
(`response_format={"type": "json_schema", ...}`). If the deployment rejects it,
the client degrades to JSON-object mode and validates the payload locally.
"""

from __future__ import annotations

import json
import re
import time
from collections import namedtuple
from typing import Any

from openai import BadRequestError, OpenAI

import config
import database
from . import budget


class IntelligenceError(RuntimeError):
    pass


ChatResult = namedtuple("ChatResult", ["data", "mode"])


def _client() -> OpenAI:
    key = budget.api_key()
    if not key:
        raise IntelligenceError(
            "No Cerebras API key: paste one in the sidebar 'AI provider & spend' panel."
        )
    return OpenAI(
        api_key=key,
        base_url=config.CEREBRAS_BASE_URL,
        timeout=config.MODEL_TIMEOUT_SECONDS,
        max_retries=1,
    )


def _bill(response: Any, model_id: str, purpose: str, prompt: str, completion: str) -> None:
    """Record real token usage (or a local estimate) against the balance."""
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if prompt_tokens <= 0:
        prompt_tokens = budget.estimate_tokens(prompt)
    if completion_tokens <= 0:
        completion_tokens = budget.estimate_tokens(completion)
    budget.record_usage(model_id, prompt_tokens, completion_tokens, purpose)


def _short_error(exc: Exception) -> str:
    """Compact error text (Cloudflare/HTML bodies can be megabytes)."""
    text = str(exc)
    # Cloudflare challenge pages arrive as HTML with inline CSS and JS. Keep the
    # readable part only, otherwise CSS rules leak into stored thesis text.
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.split("{", 1)[0]
    text = re.sub(r"Attention Required!?\s*\|\s*Cloudflare", "Cloudflare challenge", text)
    text = re.sub(r"\b(?:if|function|var|let|const|window|document)\b.*$", "", text)
    text = re.sub(r"\s+(?:body|html|head|div)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" |,;")
    return f"{type(exc).__name__}: {text[:240]}"


def _close_truncated(text: str) -> str:
    """Best-effort completion of JSON cut off by the max_tokens limit."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()

    repaired = text
    if in_string:
        repaired += '"'
    repaired = repaired.rstrip()
    while repaired.endswith(","):
        repaired = repaired[:-1].rstrip()
    for opener in reversed(stack):
        repaired += "}" if opener == "{" else "]"
    return repaired


def _parse_json(content: str) -> dict[str, Any]:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    truncated = False
    for candidate in (content, _close_truncated(content)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            truncated = True
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            pass
    hint = " (output was truncated by max_tokens)" if truncated else ""
    raise IntelligenceError(f"Model returned non-JSON content{hint}: {content[:200]}")


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def validate_schema(data: Any, schema: dict, path: str = "$") -> None:
    """Minimal JSON-Schema check for required keys, types and enums."""
    expected = schema.get("type")
    if expected:
        py_type = _TYPE_MAP.get(expected)
        if py_type:
            if expected in ("number", "integer") and isinstance(data, bool):
                raise ValueError(f"{path}: expected {expected}, got boolean")
            if not isinstance(data, py_type):
                raise ValueError(f"{path}: expected {expected}, got {type(data).__name__}")
    if "enum" in schema and data not in schema["enum"]:
        raise ValueError(f"{path}: {data!r} not in {schema['enum']}")

    if expected == "object":
        for key in schema.get("required", []):
            if key not in data:
                raise ValueError(f"{path}: missing required key '{key}'")
        for key, child in (schema.get("properties") or {}).items():
            if key in data:
                validate_schema(data[key], child, f"{path}.{key}")
    elif expected == "array" and schema.get("items") is not None:
        for index, item in enumerate(data):
            validate_schema(item, schema["items"], f"{path}[{index}]")


def chat(
    system: str,
    user: str,
    purpose: str = "analysis",
    temperature: float = 0.2,
    max_tokens: int = 4000,
    schema: dict | None = None,
    schema_name: str = "response",
) -> ChatResult:
    """Run one completion, preferring strict schema mode, with graceful fallback."""
    client = _client()
    model_id = budget.model()
    started = time.perf_counter()
    attempts: list[tuple[dict | None, str]] = []
    if schema:
        attempts.append(
            (
                {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                "json_schema",
            )
        )
    attempts.extend([({"type": "json_object"}, "json_object"), (None, "plain")])

    last_error = ""
    for response_format, mode in attempts:
        try:
            kwargs: dict[str, Any] = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            data = _parse_json(content)
            if schema:
                validate_schema(data, schema)
            _bill(response, model_id, purpose, system + user, content)
            database.log_model_run(
                purpose=f"{purpose}:{mode}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=True,
                model=model_id,
            )
            return ChatResult(data, mode)
        except BadRequestError as exc:
            last_error = f"schema rejected: {_short_error(exc)}"
            continue
        except ValueError as exc:
            # Local schema validation failed (e.g. salvaged truncated JSON);
            # fall through to the next, looser response mode.
            last_error = f"schema mismatch: {exc}"
            continue
        except Exception as exc:
            last_error = _short_error(exc)
            break

    database.log_model_run(
        purpose=purpose,
        latency_ms=int((time.perf_counter() - started) * 1000),
        ok=False,
        error=last_error,
        model=model_id,
    )
    raise IntelligenceError(last_error or "Model call failed")


def chat_json(
    system: str,
    user: str,
    purpose: str = "analysis",
    temperature: float = 0.2,
    max_tokens: int = 4000,
) -> dict[str, Any]:
    return chat(
        system, user, purpose=purpose, temperature=temperature, max_tokens=max_tokens
    ).data


def chat_text(
    system: str,
    user: str,
    purpose: str = "consultant",
    temperature: float = 0.3,
    max_tokens: int = 1200,
) -> str:
    """Free-form markdown completion for the follow-up consultant chat.

    The analyst pipeline is JSON-only, but a question/answer conversation is
    more useful as prose, so this call deliberately skips response_format.
    """
    client = _client()
    model_id = budget.model()
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = (response.choices[0].message.content or "").strip()
        _bill(response, model_id, purpose, system + user, content)
        database.log_model_run(
            purpose=purpose,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=True,
            model=model_id,
        )
        return content
    except Exception as exc:
        error = _short_error(exc)
        database.log_model_run(
            purpose=purpose,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=False,
            error=error,
            model=model_id,
        )
        raise IntelligenceError(error)


def ping() -> dict[str, Any]:
    """Tiny round-trip used by verification and dashboard health."""
    return chat_json(
        system="You are a health check. Reply only with JSON.",
        user='Return exactly {"ok": true, "model": "' + budget.model() + '"}',
        purpose="ping",
        temperature=0.0,
        max_tokens=100,
    )
