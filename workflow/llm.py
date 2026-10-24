from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel


SchemaT = TypeVar("SchemaT", bound=BaseModel)
_log_lock = threading.Lock()
PROJECT_DIR = Path(__file__).resolve().parent


class StructuredOutputError(ValueError):
    def __init__(self, message: str, usage: dict[str, Any]) -> None:
        super().__init__(message)
        self.usage = usage
        self.error_category = "structured_output"


class LLMProviderError(RuntimeError):
    def __init__(self, message: str, usage: dict[str, Any], error_category: str) -> None:
        super().__init__(message)
        self.usage = usage
        self.error_category = error_category


@lru_cache(maxsize=1)
def get_chat_model():
    provider = os.getenv("WORKFLOW_LLM_PROVIDER", "siliconflow").strip().lower()
    if provider in {"siliconflow", "openai_compatible"}:
        from langchain_openai import ChatOpenAI

        if not os.getenv("SILICONFLOW_API_KEY"):
            raise RuntimeError("缺少 SILICONFLOW_API_KEY；请在项目根目录 .env 中配置")
        return ChatOpenAI(
            model=os.getenv("WORKFLOW_CHAT_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash"),
            base_url=os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
            api_key=os.getenv("SILICONFLOW_API_KEY"),
            temperature=0,
            timeout=float(os.getenv("WORKFLOW_LLM_TIMEOUT", "120")),
            max_retries=int(os.getenv("WORKFLOW_LLM_MAX_RETRIES", "1")),
        )

    from langchain_community.chat_models.tongyi import ChatTongyi

    if not os.getenv("DASHSCOPE_API_KEY"):
        raise RuntimeError("缺少 DASHSCOPE_API_KEY；请在项目根目录 .env 中配置通义模型密钥")
    return ChatTongyi(model=os.getenv("WORKFLOW_CHAT_MODEL", "qwen-plus"))


def _model_label() -> str:
    provider = os.getenv("WORKFLOW_LLM_PROVIDER", "siliconflow").strip().lower()
    if provider in {"siliconflow", "openai_compatible"}:
        return f"siliconflow:{os.getenv('WORKFLOW_CHAT_MODEL', 'deepseek-ai/DeepSeek-V4.1-Flash')}"
    return f"dashscope:{os.getenv('WORKFLOW_CHAT_MODEL', 'qwen-plus')}"


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def invoke_structured(
    role: str,
    schema: type[SchemaT],
    system_prompt: str,
    user_prompt: str,
) -> tuple[SchemaT, dict[str, int | float | None]]:
    started = time.perf_counter()
    raw: BaseMessage | None = None
    received_response = False
    try:
        result = get_chat_model().with_structured_output(schema, include_raw=True).invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        received_response = True
        raw = result.get("raw")
        parsed = result.get("parsed")
        if result.get("parsing_error"):
            raise ValueError(f"模型结构化输出解析失败：{result['parsing_error']}")
        if parsed is None:
            raise ValueError("模型没有返回结构化结果")
        if isinstance(parsed, schema):
            output = parsed
        else:
            output = schema.model_validate(parsed)
        return output, _record_usage(role, raw, started, "success")
    except Exception as exc:
        usage = _record_usage(role, raw, started, "error", str(exc))
        message = _safe_error(str(exc))
        if received_response and isinstance(exc, (ValueError, TypeError)):
            raise StructuredOutputError(message, usage) from exc
        category = (
            "timeout"
            if isinstance(exc, TimeoutError)
            or "timeout" in type(exc).__name__.lower()
            or "timed out" in str(exc).lower()
            else "provider_or_system"
        )
        raise LLMProviderError(message, usage, category) from exc


def _safe_error(message: str) -> str:
    import re

    message = re.sub(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]?\s*\S+", r"\1=[REDACTED]", message)
    message = re.sub(r"(?i)sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)
    return message[:400]


def _record_usage(
    role: str,
    raw: BaseMessage | None,
    started: float,
    outcome: str,
    error: str | None = None,
) -> dict[str, Any]:
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    usage = getattr(raw, "usage_metadata", None) or {}
    if not usage:
        metadata = getattr(raw, "response_metadata", None) or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    record: dict[str, Any] = {
        "role": role,
        "elapsed_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "outcome": outcome,
    }
    if error:
        record["error"] = _safe_error(error)

    log_path = Path(os.getenv("WORKFLOW_LLM_LOG", PROJECT_DIR / "logs" / "workflow_llm_calls.jsonl"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        **record,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": _model_label(),
    }
    with _log_lock:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")
    return record
