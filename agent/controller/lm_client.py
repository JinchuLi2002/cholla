"""Minimal OpenAI/Azure OpenAI JSON client for planner/summarizer advisory calls."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from agent.controller.specs import SchemaValidationError, validate_json_schema

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AZURE_OPENAI_API_VERSION = "2024-12-01-preview"
PROVIDER_OPENAI = "openai"
PROVIDER_AZURE = "azure"
JSON_ONLY_INSTRUCTION = (
    "You must return exactly one JSON object and nothing else. "
    "Do not use markdown code fences. "
    "Do not add explanation text before or after the JSON."
)


class LMClientError(RuntimeError):
    """Raised when LM invocation or strict JSON parsing fails."""


def _error_text(code: str, detail: str) -> str:
    return f"lm_client_error[{code}]: {detail}"


def _normalize_base_url(raw_value: str) -> str:
    normalized = raw_value.strip()
    if not normalized:
        return ""

    for suffix in ("/openai/v1/chat/completions", "/chat/completions"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized.rstrip("/")


def _normalize_provider(raw_value: str) -> str:
    normalized = raw_value.strip().lower()
    aliases = {
        PROVIDER_OPENAI: PROVIDER_OPENAI,
        PROVIDER_AZURE: PROVIDER_AZURE,
        "azure_openai": PROVIDER_AZURE,
        "azureopenai": PROVIDER_AZURE,
    }
    return aliases.get(normalized, "")


def _infer_provider(*, provider_override: str | None, base_url_override: str | None) -> str:
    candidates: list[str] = []
    if isinstance(provider_override, str):
        candidates.append(provider_override)
    candidates.extend(
        [
            os.environ.get("ASTROMLAB_LM_PROVIDER", ""),
            os.environ.get("ASTROMLAB_PROVIDER", ""),
            os.environ.get("OPENAI_PROVIDER", ""),
        ]
    )
    for candidate in candidates:
        normalized = _normalize_provider(candidate)
        if normalized:
            return normalized
        if candidate.strip():
            raise LMClientError(
                _error_text(
                    "invalid_provider",
                    f"unsupported provider {candidate!r}; expected one of ['openai', 'azure']",
                )
            )

    endpoint_hint = (
        base_url_override.strip()
        if isinstance(base_url_override, str) and base_url_override.strip()
        else (
            os.environ.get("ASTROMLAB_ENDPOINT", "").strip()
            or os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
        )
    ).lower()
    if ".cognitiveservices.azure.com" in endpoint_hint or ".openai.azure.com" in endpoint_hint:
        return PROVIDER_AZURE

    if (
        os.environ.get("ASTROMLAB_API_VERSION", "").strip()
        or os.environ.get("AZURE_OPENAI_API_VERSION", "").strip()
    ):
        return PROVIDER_AZURE
    return PROVIDER_OPENAI


def _chat_completions_url(*, provider: str, base_url: str, api_version: str) -> str:
    trimmed = base_url.rstrip("/")
    if provider == PROVIDER_AZURE:
        if trimmed.endswith("/openai/v1"):
            raw_url = f"{trimmed}/chat/completions"
        else:
            raw_url = f"{trimmed}/openai/v1/chat/completions"
        query = urllib_parse.urlencode({"api-version": api_version})
        return f"{raw_url}?{query}"

    if trimmed.endswith("/v1"):
        return f"{trimmed}/chat/completions"
    return f"{trimmed}/v1/chat/completions"


def _resolve_api_config(
    *,
    api_key_override: str | None,
    base_url_override: str | None,
    provider_override: str | None,
    api_version_override: str | None,
) -> tuple[str, str, str, str]:
    provider = _infer_provider(
        provider_override=provider_override,
        base_url_override=base_url_override,
    )

    api_key = api_key_override.strip() if isinstance(api_key_override, str) else ""
    if not api_key:
        api_key = (
            os.environ.get("ASTROMLAB_API_KEY", "").strip()
            or os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
    if not api_key:
        raise LMClientError(
            _error_text(
                "missing_api_key",
                "ASTROMLAB_API_KEY, AZURE_OPENAI_API_KEY, or OPENAI_API_KEY is required for call_openai_json",
            )
        )

    if provider == PROVIDER_AZURE:
        endpoint = base_url_override.strip() if isinstance(base_url_override, str) else ""
        if not endpoint:
            endpoint = (
                os.environ.get("ASTROMLAB_ENDPOINT", "").strip()
                or os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
                or os.environ.get("OPENAI_BASE_URL", "").strip()
            )
        normalized_endpoint = _normalize_base_url(endpoint)
        if not normalized_endpoint:
            raise LMClientError(
                _error_text(
                    "missing_base_url",
                    "ASTROMLAB_ENDPOINT, AZURE_OPENAI_ENDPOINT, or OPENAI_BASE_URL is required for azure provider",
                )
            )

        api_version = api_version_override.strip() if isinstance(api_version_override, str) else ""
        if not api_version:
            api_version = (
                os.environ.get("ASTROMLAB_API_VERSION", "").strip()
                or os.environ.get("AZURE_OPENAI_API_VERSION", "").strip()
                or os.environ.get("OPENAI_API_VERSION", "").strip()
                or DEFAULT_AZURE_OPENAI_API_VERSION
            )
        return (provider, api_key, normalized_endpoint, api_version)

    base_url = base_url_override.strip() if isinstance(base_url_override, str) else ""
    if not base_url:
        base_url = (
            os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("ASTROMLAB_ENDPOINT", "").strip()
            or DEFAULT_OPENAI_BASE_URL
        )
    normalized_base_url = _normalize_base_url(base_url)
    if not normalized_base_url:
        normalized_base_url = DEFAULT_OPENAI_BASE_URL
    return (provider, api_key, normalized_base_url, "")


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
    provider: str,
    api_key: str,
    base_url: str,
    api_version: str,
    model: str,
    temperature: float,
    messages: list[dict[str, str]],
) -> str:
    if provider == PROVIDER_AZURE:
        try:
            from openai import AzureOpenAI
        except Exception as exc:  # noqa: BLE001
            raise LMClientError(_error_text("sdk_unavailable", str(exc))) from exc
        client = AzureOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            api_version=api_version,
        )
    else:
        try:
            from openai import OpenAI
        except Exception as exc:  # noqa: BLE001
            raise LMClientError(_error_text("sdk_unavailable", str(exc))) from exc
        client = OpenAI(base_url=base_url, api_key=api_key)

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
    provider: str,
    api_key: str,
    chat_completions_url: str,
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

    headers = {"Content-Type": "application/json"}
    if provider == PROVIDER_AZURE:
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib_request.Request(
        chat_completions_url,
        data=data,
        method="POST",
        headers=headers,
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
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    api_version: str | None = None,
) -> dict[str, Any]:
    """Call OpenAI-compatible chat completions and return a strictly parsed JSON object.

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

    resolved_provider, resolved_api_key, resolved_base_url, resolved_api_version = _resolve_api_config(
        api_key_override=api_key,
        base_url_override=base_url,
        provider_override=provider,
        api_version_override=api_version,
    )
    chat_completions_url = _chat_completions_url(
        provider=resolved_provider,
        base_url=resolved_base_url,
        api_version=resolved_api_version,
    )
    messages = _messages(
        system_prompt=system_prompt,
        user_payload=user_payload,
        output_schema=output_schema,
    )

    try:
        response_text = _sdk_call(
            provider=resolved_provider,
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            api_version=resolved_api_version,
            model=model.strip(),
            temperature=temp,
            messages=messages,
        )
    except LMClientError as sdk_exc:
        # Fall back to direct HTTP only when SDK is unavailable.
        if "sdk_unavailable" not in str(sdk_exc):
            raise
        response_text = _http_call(
            provider=resolved_provider,
            api_key=resolved_api_key,
            chat_completions_url=chat_completions_url,
            model=model.strip(),
            temperature=temp,
            messages=messages,
        )

    parsed = _strict_json_object(response_text)
    _validate_output_schema(parsed, output_schema)
    return parsed


__all__ = ["LMClientError", "call_openai_json"]
