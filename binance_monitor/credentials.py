"""Local session import. Never executes cURL or reads a browser's credential store."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .core import ValidationError


ALLOWED_HEADERS = frozenset({
    "cookie", "csrftoken", "bnc-uuid", "fvideo-id", "fvideo-token", "device-info",
    "user-agent", "lang", "accept-language", "x-passthrough-token",
})


def official_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "www.binance.com"
                or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
            raise ValueError
    except ValueError:
        raise ValidationError("请求地址必须是 www.binance.com 的 HTTPS 地址。") from None
    return value


@dataclass(frozen=True)
class Session:
    headers: dict[str, str] = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.headers, dict):
            raise ValidationError("登录参数必须是请求头对象。")
        clean = {}
        for name, value in self.headers.items():
            if not isinstance(name, str):
                raise ValidationError("请求头名称必须是字符串。")
            key = name.lower().strip()
            if key not in ALLOWED_HEADERS:
                continue
            if not isinstance(value, str) or any(c in value for c in ("\r", "\n", "\x00")) or len(value) > 100_000:
                raise ValidationError("登录参数包含非法字符或长度异常。")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                raise ValidationError("登录参数不是有效的 HTTP 请求头；请重新从浏览器导入。") from None
            if value.strip():
                clean[key] = value.strip()
        if not clean.get("cookie") or not clean.get("csrftoken"):
            raise ValidationError("需要 Cookie 与 csrftoken，请在本机导入已登录币安的请求头。")
        object.__setattr__(self, "headers", clean)

    def __repr__(self):
        return "Session(<redacted>)"

    @property
    def fingerprint(self) -> str:
        # No identity is guessed from analytics/device cookies. A changed login is
        # conservatively a different owner until authenticated identity is available.
        canonical = json.dumps({key: self.headers[key] for key in ("cookie", "csrftoken")}, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImportedRequest:
    session: Session
    url: str
    method: str
    body: dict | None


def import_request(text: str) -> ImportedRequest:
    """Accept header JSON or Chrome/Edge's Copy as cURL (bash), as inert data."""
    if len(text) > 300_000:
        raise ValidationError("导入内容过长。")
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError
            raw_headers = data.get("headers", data)
            if not isinstance(raw_headers, dict):
                raise ValueError
            session = Session(raw_headers)
            url = official_url(data.get("url", "https://www.binance.com/"))
            body = data.get("body")
            if body is not None and not isinstance(body, dict):
                raise ValueError
            return ImportedRequest(session, url, str(data.get("method", "GET")).upper(), body)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise ValidationError("请求头 JSON 格式不正确，必须包含 Cookie 和 csrftoken。") from None
    try:
        parts = shlex.split(text.replace("\\\r\n", "").replace("\\\n", "").replace("^\r\n", "").replace("^\n", ""))
    except ValueError:
        raise ValidationError("无法解析 cURL；请选择浏览器的“Copy as cURL (bash)”。") from None
    if not parts or parts[0].lower() not in {"curl", "curl.exe"}:
        raise ValidationError("仅支持请求头 JSON 或浏览器复制的 cURL (bash)；不会执行命令。")
    headers: dict[str, str] = {}
    url = ""
    method = "GET"
    body = None
    index = 1
    while index < len(parts):
        part = parts[index]
        if part in {"-H", "--header", "-b", "--cookie", "-X", "--request", "--data", "--data-raw", "--data-binary", "--url"}:
            index += 1
            if index >= len(parts):
                raise ValidationError("cURL 参数不完整。")
            value = parts[index]
            if part in {"-H", "--header"}:
                name, sep, content = value.partition(":")
                if not sep:
                    # cURL uses "Name;" to send an explicitly empty header.
                    # Edge exports this syntax for optional Binance headers.
                    if not value.endswith(";") or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value[:-1]):
                        raise ValidationError("cURL 请求头格式不正确。")
                    name, content = value[:-1], ""
                headers[name.lower()] = content.strip()
            elif part in {"-b", "--cookie"}:
                if "=" not in value:
                    raise ValidationError("Cookie 必须是请求头内容，不能是文件名。")
                headers["cookie"] = value
            elif part in {"-X", "--request"}:
                method = value.upper()
            elif part == "--url":
                url = official_url(value)
            else:
                if value.startswith("@"):
                    raise ValidationError("不读取 cURL 引用的文件。")
                try:
                    body = json.loads(value)
                    if not isinstance(body, dict):
                        raise ValueError
                except (ValueError, TypeError):
                    raise ValidationError("仅支持 JSON 对象形式的请求体。") from None
                if method == "GET":
                    method = "POST"
        elif part.startswith("https://"):
            if url:
                raise ValidationError("只允许导入一个请求地址。")
            url = official_url(part)
        elif part not in {"--compressed", "--location", "-L", "--silent", "-s", "--globoff"}:
            # Reject shell operators and options that could hide files, proxies or extra destinations.
            raise ValidationError("cURL 包含不支持的参数；请使用浏览器原始 Copy as cURL (bash)。")
        index += 1
    if not url:
        raise ValidationError("cURL 缺少币安请求地址。")
    return ImportedRequest(Session(headers), url, method, body)


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise ValidationError("加密保存只支持 Windows；可仅在内存中使用登录参数。")
    buffer = ctypes.create_string_buffer(data)
    incoming = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Never allow a platform UI prompt; bind encryption to the current Windows user.
    if decrypt:
        success = crypt32.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        success = crypt32.CryptProtectData(ctypes.byref(incoming), "BinanceSlotMonitor", None, None, None, 1, ctypes.byref(outgoing))
    if not success:
        raise ValidationError("Windows 加密操作失败；请使用当前用户重新导入登录参数。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(outgoing.data)


def save_session(session: Session, path: Path):
    encrypted = _dpapi(json.dumps(session.headers).encode("utf-8"), decrypt=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(encrypted)
    temp.replace(path)


def load_session(path: Path) -> Session:
    try:
        decoded = _dpapi(path.read_bytes(), decrypt=True)
        return Session(json.loads(decoded))
    except (OSError, ValueError, TypeError):
        raise ValidationError("无法读取加密登录参数，请在本机重新导入。") from None
