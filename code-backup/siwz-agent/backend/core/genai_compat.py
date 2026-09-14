"""Compatibility layer for the current ``google-genai`` SDK.

The application historically used ``google-generativeai``. This module keeps
the small API surface used by the backend while routing every request through
``google.genai.Client``.
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from typing import Any

from google import genai as _genai
from google.genai import types as _types


# dostrojenie 2026-09-14: stary SDK przyjmował części multimodalne jako
# [prompt_str, {"mime_type": "image/png", "data": bytes}, ...]. Nowy google-genai
# nie zna takiego dict-a → "31 validation errors for _GenerateContentParameters"
# (od migracji 18.08 padał KAŻDY OCR vision: otwarcia ofert, audyt dokumentacji).
# Tłumaczymy stare formaty na types.Part i pakujemy w jeden Content użytkownika.
def _to_part(x: Any) -> Any:
    if isinstance(x, _types.Part):
        return x
    if isinstance(x, str):
        return _types.Part.from_text(text=x)
    if isinstance(x, dict):
        if "mime_type" in x and "data" in x:
            data = x["data"]
            if isinstance(data, str):
                data = base64.b64decode(data)
            return _types.Part.from_bytes(data=bytes(data), mime_type=x["mime_type"])
        if "inline_data" in x and isinstance(x["inline_data"], dict):
            d = x["inline_data"]; data = d.get("data")
            if isinstance(data, str):
                data = base64.b64decode(data)
            return _types.Part.from_bytes(data=bytes(data), mime_type=d.get("mime_type", "application/octet-stream"))
        if "file_data" in x and isinstance(x["file_data"], dict):
            d = x["file_data"]
            return _types.Part.from_uri(file_uri=d.get("file_uri") or d.get("uri"), mime_type=d.get("mime_type"))
        if "text" in x:
            return _types.Part.from_text(text=str(x["text"]))
        return _types.Part(**x)
    if isinstance(x, _types.File) or (hasattr(x, "uri") and hasattr(x, "mime_type")):
        return _types.Part.from_uri(file_uri=x.uri, mime_type=x.mime_type)
    try:  # obraz PIL
        from PIL import Image  # noqa: WPS433
        if isinstance(x, Image.Image):
            buf = io.BytesIO(); x.save(buf, format="PNG")
            return _types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")
    except Exception:
        pass
    return x


def _normalize_contents(contents: Any) -> Any:
    """str / Content / lista Content — bez zmian; lista części (stary styl) → 1 Content."""
    if isinstance(contents, (str, _types.Content)):
        return contents
    if isinstance(contents, (list, tuple)):
        flat: list = []
        for it in contents:
            if isinstance(it, (list, tuple)):
                flat.extend(it)
            else:
                flat.append(it)
        if not flat:
            return contents
        if all(isinstance(i, _types.Content) for i in flat):
            return flat
        if all(isinstance(i, dict) and "role" in i for i in flat):   # stara historia czatu
            return [_types.Content(role=i["role"], parts=[_to_part(pp) for pp in (i.get("parts") or [])]) for i in flat]
        return _types.Content(role="user", parts=[_to_part(i) for i in flat])
    return contents


_api_key: str | None = None
_clients: dict[str, _genai.Client] = {}


def configure(*, api_key: str) -> None:
    global _api_key
    _api_key = api_key


def _client() -> _genai.Client:
    if not _api_key:
        raise RuntimeError("Gemini API key is not configured")
    client = _clients.get(_api_key)
    if client is None:
        client = _genai.Client(api_key=_api_key)
        _clients[_api_key] = client
    return client


class GenerativeModel:
    """Old-style model facade backed by ``Client.models``."""

    def __init__(
        self,
        model_name: str,
        generation_config: Any | None = None,
        system_instruction: str | None = None,
        **_: Any,
    ) -> None:
        self.model_name = model_name
        self.generation_config = generation_config
        self.system_instruction = system_instruction

    def _config(self, override: Any | None) -> _types.GenerateContentConfig | None:
        config = override if override is not None else self.generation_config
        if config is None and self.system_instruction is None:
            return None
        if isinstance(config, _types.GenerateContentConfig):
            values = config.model_dump(exclude_none=True)
        elif isinstance(config, dict):
            values = dict(config)
        elif config is None:
            values = {}
        else:
            values = {
                name: getattr(config, name)
                for name in _types.GenerateContentConfig.model_fields
                if getattr(config, name, None) is not None
            }
        if self.system_instruction is not None:
            values.setdefault("system_instruction", self.system_instruction)
        return _types.GenerateContentConfig(**values)

    def generate_content(
        self,
        contents: Any,
        generation_config: Any | None = None,
        **_: Any,
    ) -> _types.GenerateContentResponse:
        return _client().models.generate_content(
            model=self.model_name,
            contents=_normalize_contents(contents),
            config=self._config(generation_config),
        )

    async def generate_content_async(
        self,
        contents: Any,
        generation_config: Any | None = None,
        **_: Any,
    ) -> _types.GenerateContentResponse:
        return await _client().aio.models.generate_content(
            model=self.model_name,
            contents=_normalize_contents(contents),
            config=self._config(generation_config),
        )


def upload_file(
    path: str,
    *,
    mime_type: str | None = None,
    display_name: str | None = None,
) -> _types.File:
    config = _types.UploadFileConfig(mime_type=mime_type, display_name=display_name)
    return _client().files.upload(file=path, config=config)


def get_file(name: str) -> _types.File:
    return _client().files.get(name=name)


types = SimpleNamespace(GenerationConfig=_types.GenerateContentConfig, Part=_types.Part)
protos = SimpleNamespace(Part=_types.Part, Blob=_types.Blob)
GenerationConfig = _types.GenerateContentConfig
