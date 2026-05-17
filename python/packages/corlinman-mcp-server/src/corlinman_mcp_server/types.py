"""JSON-RPC 2.0 envelope and MCP 2024-11-05 capability payloads.

Wire types only — no transport, no async. Mirrors the Rust ``schema``
module 1:1: every payload here serialises to the same on-the-wire JSON
shape as the Rust crate, so a client speaking to either implementation
sees identical frames.

Reference: <https://spec.modelcontextprotocol.io/specification/2024-11-05/>
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

JSONRPC_VERSION: Literal["2.0"] = "2.0"
"""JSON-RPC 2.0 protocol version literal. Required on every frame."""

MCP_PROTOCOL_VERSION: Literal["2024-11-05"] = "2024-11-05"
"""MCP protocol version we implement."""

JsonValue = Any
"""Loose alias for any JSON value. Kept ``Any`` so payloads can carry
arbitrary plugin-supplied schemas without forcing a discriminated union."""


# ---------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------


class error_codes:  # noqa: N801 — namespace mirror of Rust `pub mod error_codes`
    """Standard JSON-RPC 2.0 error codes (§5.1) plus MCP / corlinman
    extensions in the implementation-defined range (-32000..=-32099).
    """

    PARSE_ERROR: int = -32700
    INVALID_REQUEST: int = -32600
    METHOD_NOT_FOUND: int = -32601
    INVALID_PARAMS: int = -32602
    INTERNAL_ERROR: int = -32603

    # corlinman MCP extensions.
    TOOL_NOT_ALLOWED: int = -32001
    SESSION_NOT_INITIALIZED: int = -32002


# ---------------------------------------------------------------------
# JSON-RPC envelope
# ---------------------------------------------------------------------


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object."""

    model_config = ConfigDict(extra="forbid")

    code: int
    message: str
    data: JsonValue | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out = handler(self)
        if self.data is None:
            out.pop("data", None)
        return out

    @classmethod
    def new(cls, code: int, message: str) -> JsonRpcError:
        return cls(code=code, message=message)

    def with_data(self, data: JsonValue) -> JsonRpcError:
        return JsonRpcError(code=self.code, message=self.message, data=data)


class JsonRpcRequest(BaseModel):
    """JSON-RPC request frame.

    Per spec, ``id`` is ``string | number | null``. A *missing* id makes
    the frame a notification (no response expected). We model that as
    ``id=None`` and elide the field on serialisation so a notification
    round-trips with the same shape it arrived in.
    """

    model_config = ConfigDict(extra="ignore")

    jsonrpc: str = JSONRPC_VERSION
    id: JsonValue | None = None
    method: str
    params: JsonValue = None

    @field_validator("jsonrpc")
    @classmethod
    def _check_jsonrpc(cls, v: str) -> str:
        if v != JSONRPC_VERSION:
            raise ValueError(f'expected jsonrpc="{JSONRPC_VERSION}", got {v!r}')
        return v

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out = handler(self)
        # JSON-RPC distinguishes "missing id" (notification) from
        # "id: null" — preserve that on the wire. We use a sentinel
        # marker on the model: id stored as None always means notification.
        if self.id is None and not self._explicit_id_null:
            out.pop("id", None)
        return out

    # The pydantic field ``id`` is allowed to be ``None``. We can't tell
    # apart "missing in input" vs "explicit null" without a sentinel; in
    # practice the server treats them identically (both -> notification).
    # Round-trip: build with ``id=None`` ⇒ field elided on dump.
    _explicit_id_null: bool = False

    def is_notification(self) -> bool:
        """True when ``id`` is absent — i.e. a notification per
        JSON-RPC 2.0 §4."""
        return self.id is None


# JsonRpcResponse is an untagged union: a frame carries *either* result
# *or* error, never both. We model it with two distinct classes plus a
# parser helper that picks the right one based on the keys present.


class JsonRpcResultResponse(BaseModel):
    """Successful JSON-RPC response frame."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: str = JSONRPC_VERSION
    id: JsonValue
    result: JsonValue

    @field_validator("jsonrpc")
    @classmethod
    def _check_jsonrpc(cls, v: str) -> str:
        if v != JSONRPC_VERSION:
            raise ValueError(f'expected jsonrpc="{JSONRPC_VERSION}", got {v!r}')
        return v


class JsonRpcErrorResponse(BaseModel):
    """Error JSON-RPC response frame."""

    model_config = ConfigDict(extra="forbid")

    jsonrpc: str = JSONRPC_VERSION
    id: JsonValue
    error: JsonRpcError

    @field_validator("jsonrpc")
    @classmethod
    def _check_jsonrpc(cls, v: str) -> str:
        if v != JSONRPC_VERSION:
            raise ValueError(f'expected jsonrpc="{JSONRPC_VERSION}", got {v!r}')
        return v


class JsonRpcResponse:
    """Façade matching the Rust ``JsonRpcResponse`` enum.

    Pydantic doesn't natively round-trip *untagged* unions back through
    serialise → deserialise cleanly, so we keep the two response shapes
    distinct (:class:`JsonRpcResultResponse` / :class:`JsonRpcErrorResponse`)
    and provide the canonical constructors + a parser on this façade.
    """

    @staticmethod
    def ok(id: JsonValue, result: JsonValue) -> JsonRpcResultResponse:
        return JsonRpcResultResponse(jsonrpc=JSONRPC_VERSION, id=id, result=result)

    @staticmethod
    def err(id: JsonValue, error: JsonRpcError) -> JsonRpcErrorResponse:
        return JsonRpcErrorResponse(jsonrpc=JSONRPC_VERSION, id=id, error=error)

    @staticmethod
    def parse(value: dict[str, Any]) -> JsonRpcResultResponse | JsonRpcErrorResponse:
        """Parse a wire-format dict into the matching response variant."""
        if "result" in value and "error" in value:
            raise ValueError("result and error are mutually exclusive")
        if "result" in value:
            return JsonRpcResultResponse.model_validate(value)
        if "error" in value:
            return JsonRpcErrorResponse.model_validate(value)
        raise ValueError("response frame must carry either result or error")


def response_id(resp: JsonRpcResultResponse | JsonRpcErrorResponse) -> JsonValue:
    """Mirror of the Rust ``JsonRpcResponse::id`` helper."""
    return resp.id


# ---------------------------------------------------------------------
# `initialize` handshake payloads
# ---------------------------------------------------------------------


class Implementation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str


class ClientCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sampling: JsonValue | None = None
    roots: JsonValue | None = None
    experimental: JsonValue | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out = handler(self)
        for k in ("sampling", "roots", "experimental"):
            if out.get(k) is None:
                out.pop(k, None)
        return out


class InitializeParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    protocol_version: str = Field(alias="protocolVersion")
    capabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)
    client_info: Implementation = Field(alias="clientInfo")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        # Always emit camelCase on the wire.
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities.model_dump(),
            "clientInfo": self.client_info.model_dump(),
        }


class ToolsCapability(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    list_changed: bool | None = Field(default=None, alias="listChanged")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.list_changed is not None:
            out["listChanged"] = self.list_changed
        return out


class ResourcesCapability(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    subscribe: bool | None = None
    list_changed: bool | None = Field(default=None, alias="listChanged")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.subscribe is not None:
            out["subscribe"] = self.subscribe
        if self.list_changed is not None:
            out["listChanged"] = self.list_changed
        return out


class PromptsCapability(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    list_changed: bool | None = Field(default=None, alias="listChanged")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.list_changed is not None:
            out["listChanged"] = self.list_changed
        return out


class ServerCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tools: ToolsCapability | None = None
    resources: ResourcesCapability | None = None
    prompts: PromptsCapability | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.tools is not None:
            out["tools"] = self.tools.model_dump()
        if self.resources is not None:
            out["resources"] = self.resources.model_dump()
        if self.prompts is not None:
            out["prompts"] = self.prompts.model_dump()
        return out


class InitializeResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    protocol_version: str = Field(alias="protocolVersion")
    capabilities: ServerCapabilities
    server_info: Implementation = Field(alias="serverInfo")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities.model_dump(),
            "serverInfo": self.server_info.model_dump(),
        }


# ---------------------------------------------------------------------
# tools/* payloads
# ---------------------------------------------------------------------


class ToolDescriptor(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    description: str | None = None
    input_schema: JsonValue = Field(alias="inputSchema")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description is not None:
            out["description"] = self.description
        out["inputSchema"] = self.input_schema
        return out


class ToolsListResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    tools: list[ToolDescriptor]
    next_cursor: str | None = Field(default=None, alias="nextCursor")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"tools": [t.model_dump() for t in self.tools]}
        if self.next_cursor is not None:
            out["nextCursor"] = self.next_cursor
        return out


class ToolsCallParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: JsonValue = None


class TextContent(BaseModel):
    """``Content`` block of type ``text``."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    """``Content`` block of type ``image``."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    type: Literal["image"] = "image"
    data: str
    mime_type: str = Field(alias="mimeType")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        return {"type": "image", "data": self.data, "mimeType": self.mime_type}


Content = TextContent | ImageContent


def text_content(text: str) -> TextContent:
    """Constructor mirror of the Rust ``Content::text`` helper."""
    return TextContent(type="text", text=text)


class ToolsCallResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    content: list[Content]
    is_error: bool = Field(default=False, alias="isError")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        return {
            "content": [c.model_dump() for c in self.content],
            "isError": self.is_error,
        }


# ---------------------------------------------------------------------
# resources/* payloads
# ---------------------------------------------------------------------


class ResourcesListParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cursor: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out = handler(self)
        if self.cursor is None:
            out.pop("cursor", None)
        return out


class Resource(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.description is not None:
            out["description"] = self.description
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        return out


class ResourcesListResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    resources: list[Resource]
    next_cursor: str | None = Field(default=None, alias="nextCursor")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"resources": [r.model_dump() for r in self.resources]}
        if self.next_cursor is not None:
            out["nextCursor"] = self.next_cursor
        return out


class ResourcesReadParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    uri: str


class TextResourceContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    uri: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    text: str

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri}
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        out["text"] = self.text
        return out


class BlobResourceContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    uri: str
    mime_type: str | None = Field(default=None, alias="mimeType")
    blob: str

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"uri": self.uri}
        if self.mime_type is not None:
            out["mimeType"] = self.mime_type
        out["blob"] = self.blob
        return out


ResourceContent = TextResourceContent | BlobResourceContent


def text_resource(uri: str, text: str) -> TextResourceContent:
    """Constructor mirror of the Rust ``ResourceContent::text`` helper."""
    return TextResourceContent(uri=uri, text=text)


class ResourcesReadResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    contents: list[ResourceContent]

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        return {"contents": [c.model_dump() for c in self.contents]}


# ---------------------------------------------------------------------
# prompts/* payloads
# ---------------------------------------------------------------------


class PromptArgument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None
    required: bool | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description is not None:
            out["description"] = self.description
        if self.required is not None:
            out["required"] = self.required
        return out


class Prompt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None
    arguments: list[PromptArgument] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.description is not None:
            out["description"] = self.description
        if self.arguments:
            out["arguments"] = [a.model_dump() for a in self.arguments]
        return out


class PromptsListResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    prompts: list[Prompt]
    next_cursor: str | None = Field(default=None, alias="nextCursor")

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {"prompts": [p.model_dump() for p in self.prompts]}
        if self.next_cursor is not None:
            out["nextCursor"] = self.next_cursor
        return out


class PromptsGetParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: JsonValue = None


class PromptRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class PromptTextContent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["text"] = "text"
    text: str


PromptContent = PromptTextContent


def prompt_text_content(text: str) -> PromptTextContent:
    return PromptTextContent(type="text", text=text)


class PromptMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: PromptRole
    content: PromptContent

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.content.model_dump()}


class PromptsGetResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    messages: list[PromptMessage]

    @model_serializer(mode="wrap")
    def _serialize(self, handler) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.description is not None:
            out["description"] = self.description
        out["messages"] = [m.model_dump() for m in self.messages]
        return out


__all__ = [
    "BlobResourceContent",
    "ClientCapabilities",
    "Content",
    "ImageContent",
    "Implementation",
    "InitializeParams",
    "InitializeResult",
    "JSONRPC_VERSION",
    "JsonRpcError",
    "JsonRpcErrorResponse",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcResultResponse",
    "JsonValue",
    "MCP_PROTOCOL_VERSION",
    "Prompt",
    "PromptArgument",
    "PromptContent",
    "PromptMessage",
    "PromptRole",
    "PromptTextContent",
    "PromptsCapability",
    "PromptsGetParams",
    "PromptsGetResult",
    "PromptsListResult",
    "Resource",
    "ResourceContent",
    "ResourcesCapability",
    "ResourcesListParams",
    "ResourcesListResult",
    "ResourcesReadParams",
    "ResourcesReadResult",
    "ServerCapabilities",
    "TextContent",
    "TextResourceContent",
    "ToolDescriptor",
    "ToolsCallParams",
    "ToolsCallResult",
    "ToolsCapability",
    "ToolsListResult",
    "error_codes",
    "prompt_text_content",
    "response_id",
    "text_content",
    "text_resource",
]
