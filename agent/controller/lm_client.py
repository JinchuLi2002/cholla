"""Minimal OpenAI JSON client for planner/summarizer advisory calls."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request

from agent.controller.specs import SchemaValidationError, validate_json_schema

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
JSON_ONLY_INSTRUCTION = (
    "You must return exactly one JSON object and nothing else. "
    "Do not use markdown code fences. "
    "Do not add explanation text before or after the JSON."
)


class LMClientError(RuntimeError):
    """Raised when LM invocation or strict JSON parsing fails."""


def _error_text(code: str, detail: str) -> str:
    return f"lm_client_error[{code}]: {detail}"


def _require_api_key() -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise LMClientError(
            _error_text(
                "missing_api_key",
                "OPENAI_API_KEY is required for call_openai_json",
            )
        )
    return api_key


def _strict_json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise LMClientError(
            _error_text("non_text_response", f"expected string content, got {type(text).__name__}")
        )

    stripped = text.strip()
    if not stripped:
        raise LMClientError(_error_text("empty_response", "model returned empty text"))

    decoder = json.JSONDecoder()
    try:
        value, end_idx = decoder.raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise LMClientError(
            _error_text(
                "invalid_json",
                f"{exc.msg} at line={exc.lineno} col={exc.colno}",
            )
        ) from exc

    if stripped[end_idx:].strip():
        raise LMClientError(_error_text("non_json_trailing_text", "response contained extra non-JSON text"))
    if not isinstance(value, dict):
        raise LMClientError(
            _error_text("non_object_json", f"expected top-level JSON object, got {type(value).__name__}")
        )
    return dict(value)


def _response_content_text(raw_content: Any) -> str:
    if isinstance(raw_content, str):
        return raw_content

    # Some OpenAI SDK responses surface content as blocks.
    if isinstance(raw_content, list):
        parts: list[str] = []
        for item in raw_content:
            if not isinstance(item, Mapping):
                continue
            text_value = item.get("text")
            if isinstance(text_value, str):
                parts.append(text_value)
        return "".join(parts)

    return ""


def _messages(
    *,
    system_prompt: str,
    user_payload: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> list[dict[str, str]]:
    try:
        schema_json = json.dumps(dict(output_schema), sort_keys=True)
    except TypeError as exc:
        raise LMClientError(_error_text("invalid_output_schema", str(exc))) from exc

    schema_text = "\nOutput must validate against this Draft-07 JSON schema:\n" f"{schema_json}"
    system_text = f"{system_prompt.strip()}\n\n{JSON_ONLY_INSTRUCTION}{schema_text}".strip()
    try:
        user_text = json.dumps(dict(user_payload), sort_keys=True)
    except TypeError as exc:
        raise LMClientError(_error_text("invalid_user_payload_json", str(exc))) from exc
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def _validate_output_schema(payload: Mapping[str, Any], output_schema: Mapping[str, Any] | None) -> None:
    if output_schema is None:
        return
    try:
        validate_json_schema(payload, output_schema, schema_name="LMOutput")
    except SchemaValidationError as exc:
        raise LMClientError(_error_text("schema_validation_failed", str(exc))) from exc


def _sdk_call(
    *,
    api_key: str,
    model: str,
    temperature: float,
    messages: list[dict[str, str]],
) -> str:
    try:
        from openai import OpenAI
    except Exception as exc:  # noqa: BLE001
        raise LMClientError(_error_text("sdk_unavailable", str(exc))) from exc

    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001
        raise LMClientError(_error_text("request_failed", str(exc))) from exc

    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise LMClientError(_error_text("invalid_response", "missing choices[0] in SDK response"))

    first = choices[0]
    message = getattr(first, "message", None)
    raw_content = getattr(message, "content", None)
    text = _response_content_text(raw_content)
    if not text:
        raise LMClientError(_error_text("empty_content", "missing message.content in SDK response"))
    return text


def _http_call(
    *,
    api_key: str,
    model: str,
    temperature: float,
    messages: list[dict[str, str]],
) -> str:
    body = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body).encode("utf-8")

    req = urllib_request.Request(
        OPENAI_CHAT_COMPLETIONS_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=90) as resp:
            resp_text = resp.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        detail = err_body
        try:
            parsed = json.loads(err_body)
            if isinstance(parsed, Mapping):
                err = parsed.get("error")
                err_map = dict(err) if isinstance(err, Mapping) else {}
                msg = err_map.get("message")
                if isinstance(msg, str) and msg.strip():
                    detail = msg.strip()
        except Exception:  # noqa: BLE001
            pass
        raise LMClientError(_error_text(f"http_{exc.code}", detail)) from exc
    except urllib_error.URLError as exc:
        raise LMClientError(_error_text("network_error", str(exc.reason))) from exc

    try:
        payload = json.loads(resp_text)
    except json.JSONDecodeError as exc:
        raise LMClientError(_error_text("invalid_api_json", str(exc))) from exc

    if not isinstance(payload, Mapping):
        raise LMClientError(_error_text("invalid_api_payload", "expected JSON object from API"))
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise LMClientError(_error_text("invalid_api_payload", "missing choices[0] in API response"))
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise LMClientError(_error_text("invalid_api_payload", "missing message in choices[0]"))
    raw_content = message.get("content")
    text = _response_content_text(raw_content)
    if not text:
        raise LMClientError(_error_text("empty_content", "missing message.content in API response"))
    return text


def call_openai_json(
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    temperature: float = 0.0,
    *,
    output_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Call OpenAI and return a strictly parsed JSON object.

    Errors raise LMClientError with stable `lm_client_error[...]` prefixes.
    """

    if not isinstance(model, str) or not model.strip():
        raise LMClientError(_error_text("invalid_model", "model must be a non-empty string"))
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise LMClientError(_error_text("invalid_system_prompt", "system_prompt must be non-empty"))
    if not isinstance(user_payload, dict):
        raise LMClientError(_error_text("invalid_user_payload", "user_payload must be a JSON object"))
    if output_schema is None:
        raise LMClientError(_error_text("missing_output_schema", "output_schema must be provided"))

    try:
        temp = float(temperature)
    except (TypeError, ValueError) as exc:
        raise LMClientError(_error_text("invalid_temperature", str(exc))) from exc

    api_key = _require_api_key()
    messages = _messages(
        system_prompt=system_prompt,
        user_payload=user_payload,
        output_schema=output_schema,
    )

    try:
        response_text = _sdk_call(
            api_key=api_key,
            model=model.strip(),
            temperature=temp,
            messages=messages,
        )
    except LMClientError as sdk_exc:
        # Fall back to direct HTTP only when SDK is unavailable.
        if "sdk_unavailable" not in str(sdk_exc):
            raise
        response_text = _http_call(
            api_key=api_key,
            model=model.strip(),
            temperature=temp,
            messages=messages,
        )

    parsed = _strict_json_object(response_text)
    _validate_output_schema(parsed, output_schema)
    return parsed


__all__ = ["LMClientError", "call_openai_json"]
