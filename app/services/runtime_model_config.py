"""Runtime model configuration shared by the web UI and parsing agent."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..config import PROJECT_ROOT, Settings

DEFAULT_API_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL_NAME = "deepseek-chat"
MASKED_API_KEY = "********"


class ModelConfigurationError(RuntimeError):
    """Safe, user-facing model configuration failure."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _protect_api_key(api_key: str) -> dict[str, str]:
    """Protect a credential with the current Windows account when available."""
    if os.name != "nt":
        return {"scheme": "restricted-file", "value": api_key}
    raw = api_key.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(
        len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    protected = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(protected),
    ):
        raise OSError("无法使用当前 Windows 用户保护 API 密钥。")
    try:
        value = ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(protected.pbData)
    return {
        "scheme": "windows-dpapi",
        "value": base64.b64encode(value).decode("ascii"),
    }


def _unprotect_api_key(payload: dict) -> str:
    scheme = payload.get("scheme")
    value = payload.get("value", "")
    if scheme == "restricted-file" and os.name != "nt":
        return str(value)
    if scheme != "windows-dpapi" or os.name != "nt":
        return ""
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return ""
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(
        len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    clear = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(clear),
    ):
        return ""
    try:
        return ctypes.string_at(clear.pbData, clear.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(clear.pbData)


@dataclass(frozen=True)
class ModelCredentials:
    base_url: str
    model: str
    api_key: str
    source: str
    expires_on: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


def _endpoint(base_url: str) -> tuple[str, str]:
    url = base_url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ModelConfigurationError(
            "API 地址不能包含凭证、查询参数或片段。"
        )
    local = parts.scheme == "http" and parts.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if not parts.hostname or (parts.scheme != "https" and not local):
        raise ModelConfigurationError(
            "外部 API 必须使用 HTTPS；本地服务可以使用 HTTP。"
        )
    endpoint = (
        url
        if url.endswith("/chat/completions")
        else f"{url}/chat/completions"
    )
    return endpoint, parts.hostname or ""


def test_model_connection(base_url: str, model: str, api_key: str) -> str:
    """Verify the synchronous chat endpoint and JSON response mode."""
    if not model.strip():
        raise ModelConfigurationError("请填写模型名称。")
    if not api_key.strip():
        raise ModelConfigurationError("请填写 API 密钥。")
    endpoint, hostname = _endpoint(base_url)
    body = {
        "model": model.strip(),
        "messages": [
            {
                "role": "system",
                "content": "这是连通性测试。只返回 JSON 对象。",
            },
            {"role": "user", "content": '请返回 {"ok":true}。'},
        ],
        "temperature": 0,
        "max_tokens": 32,
        "response_format": {"type": "json_object"},
    }
    if hostname == "api.deepseek.com":
        body["thinking"] = {"type": "disabled"}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key.strip()}"},
                json=body,
            )
    except httpx.TimeoutException:
        raise ModelConfigurationError("连接超时，请检查网络或 API 地址。") from None
    except httpx.HTTPError:
        raise ModelConfigurationError("网络连接失败，请检查网络或 API 地址。") from None

    if response.status_code in {401, 403}:
        raise ModelConfigurationError(
            f"API 密钥无效或没有访问权限（HTTP {response.status_code}）。"
        )
    if response.status_code == 404:
        raise ModelConfigurationError("API 地址或模型不存在（HTTP 404）。")
    if response.status_code == 429:
        raise ModelConfigurationError(
            "API 请求受限或账户额度不足（HTTP 429）。"
        )
    if response.status_code == 202:
        raise ModelConfigurationError(
            "接口返回异步任务，当前系统需要同步聊天接口（HTTP 202）。"
        )
    if response.status_code != 200:
        raise ModelConfigurationError(
            f"API 返回 HTTP {response.status_code}，请检查地址、模型和接口兼容性。"
        )
    try:
        choice = response.json()["choices"][0]
        content = choice["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if not isinstance(json.loads(content), dict):
            raise TypeError("模型连接测试必须返回 JSON 对象。")
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        AttributeError,
        json.JSONDecodeError,
    ):
        raise ModelConfigurationError(
            "接口已响应，但未返回兼容的 Chat Completions JSON 结果。"
        ) from None
    return (
        f"模型 {model.strip()} 连接成功，聊天接口与 JSON 模式可用。"
    )


class RuntimeModelConfig:
    """Reuse one verified model credential for the current local calendar day."""

    def __init__(
        self,
        storage_path: Path | None = None,
        today_provider=None,
    ) -> None:
        self._lock = threading.RLock()
        self._storage_path = Path(storage_path) if storage_path else None
        self._today_provider = today_provider or date.today
        self._base_url = DEFAULT_API_BASE_URL
        self._model = DEFAULT_MODEL_NAME
        self._api_key = ""
        self._credential_date: str | None = None
        self._load_current_day()

    def _today(self) -> str:
        return self._today_provider().isoformat()

    def _clear(self, remove_file: bool = False) -> None:
        self._base_url = DEFAULT_API_BASE_URL
        self._model = DEFAULT_MODEL_NAME
        self._api_key = ""
        self._credential_date = None
        if remove_file and self._storage_path:
            self._storage_path.unlink(missing_ok=True)

    def _load_current_day(self) -> None:
        if not self._storage_path or not self._storage_path.is_file():
            return
        try:
            payload = json.loads(
                self._storage_path.read_text(encoding="utf-8")
            )
            if payload.get("date") != self._today():
                self._clear(remove_file=True)
                return
            api_key = _unprotect_api_key(payload.get("api_key", {})).strip()
            if not api_key:
                self._clear(remove_file=True)
                return
            self._base_url = (
                str(payload.get("base_url", "")).strip()
                or DEFAULT_API_BASE_URL
            )
            self._model = (
                str(payload.get("model", "")).strip() or DEFAULT_MODEL_NAME
            )
            self._api_key = api_key
            self._credential_date = str(payload["date"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._clear(remove_file=True)

    def _save_current_day(self) -> None:
        if not self._storage_path:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "date": self._credential_date,
            "base_url": self._base_url,
            "model": self._model,
            "api_key": _protect_api_key(self._api_key),
        }
        temporary = self._storage_path.with_suffix(
            self._storage_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self._storage_path)

    def _expire_if_needed(self) -> None:
        if self._credential_date and self._credential_date != self._today():
            self._clear(remove_file=True)

    def runtime_credentials(self) -> ModelCredentials | None:
        with self._lock:
            self._expire_if_needed()
            if not self._api_key:
                return None
            return ModelCredentials(
                base_url=self._base_url,
                model=self._model,
                api_key=self._api_key,
                source="runtime",
                expires_on=self._credential_date,
            )

    def current_api_key(self) -> str:
        with self._lock:
            self._expire_if_needed()
            return self._api_key

    def save_verified(self, base_url: str, model: str, api_key: str) -> None:
        with self._lock:
            clean_key = api_key.strip()
            if not clean_key:
                raise ModelConfigurationError("请填写 API 密钥。")
            self._base_url = base_url.strip() or DEFAULT_API_BASE_URL
            self._model = model.strip() or DEFAULT_MODEL_NAME
            self._api_key = clean_key
            self._credential_date = self._today()
            self._save_current_day()

    def effective(self, settings: Settings) -> ModelCredentials:
        runtime = self.runtime_credentials()
        if runtime is not None:
            return runtime
        if (
            settings.llm_enable_api
            and settings.llm_api_base
            and settings.llm_model
            and settings.llm_api_key
        ):
            return ModelCredentials(
                base_url=settings.llm_api_base.strip(),
                model=settings.llm_model.strip(),
                api_key=settings.llm_api_key.strip(),
                source="environment",
            )
        return ModelCredentials(
            base_url=DEFAULT_API_BASE_URL,
            model=DEFAULT_MODEL_NAME,
            api_key="",
            source="none",
        )


@lru_cache
def get_runtime_model_config() -> RuntimeModelConfig:
    return RuntimeModelConfig(
        PROJECT_ROOT / "data" / ".runtime" / "model-config.json"
    )
