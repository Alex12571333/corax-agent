"""Host-owned tool catalog and embedding-first per-turn selection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import keyword
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.request import Request, urlopen

from .config import ToolRoutingConfig

TOOL_SEARCH_ID = "tool.search"
TOOL_CALL_ID = "tool.call"
OBJECT_RUN_ID = "object.run"
_SAFE_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")
_PYTHON_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESULT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TRIVIAL_WORDS = frozenset(
    {
        "привет",
        "приветик",
        "прив",
        "здравствуй",
        "здравствуйте",
        "хай",
        "ку",
        "hi",
        "hello",
        "hey",
        "спасибо",
        "спс",
        "благодарю",
        "thanks",
        "thank",
        "thx",
        "ок",
        "окей",
        "ok",
        "okay",
        "угу",
        "ага",
        "ладно",
        "понятно",
        "да",
        "нет",
        "yes",
        "no",
        "пока",
        "bye",
        "лол",
        "lol",
        "ха",
        "хаха",
        "haha",
    }
)

_SEARCH_SPEC = {
    "id": TOOL_SEARCH_ID,
    "model_name": "tool_search",
    "description": (
        "Find and activate tools for one missing capability family. "
        "Repeat for each independent need when the visible tools are insufficient."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What capability is needed",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

_CALL_SPEC = {
    "id": TOOL_CALL_ID,
    "model_name": "tool_call",
    "description": (
        "Call one capability activated for this turn. Use the exact capability "
        "id and input schema from the trusted runtime tool context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "capability": {
                "type": "string",
                "description": "Exact activated capability id",
            },
            "input": {
                "type": "object",
                "description": "Arguments matching that capability's input schema",
            },
        },
        "required": ["capability", "input"],
        "additionalProperties": False,
    },
}

_OBJECT_RUN_SPEC = {
    "id": OBJECT_RUN_ID,
    "model_name": "object_run",
    "description": (
        "Execute one bounded async Python task against the available "
        "Corax object facade."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "maxLength": 32_000,
                "description": (
                    "Body of an async function using self.<group>.<method>"
                ),
            }
        },
        "required": ["code"],
        "additionalProperties": False,
    },
}

_FACADE_GROUPS = {
    "filesystem": "files",
    "editor": "files",
    "shell": "shell",
    "subagents": "agents",
}
_PYTHON_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
    "array": "list",
}
_MAX_FACADE_OUTPUT_FIELDS = 12
_MAX_FACADE_OUTPUT_CHARS = 224
_MAX_OBJECT_FACADE_GROUPS = 16
_MAX_OBJECT_FACADE_METHODS = 128
_MAX_OBJECT_FACADE_METHODS_PER_GROUP = 32
_MAX_OBJECT_SIGNATURE_CHARS = 512
_DEFAULT_OBJECT_FACADE_CHARS = 16_000
_RESERVED_OBJECT_METHODS = frozenset({"tools.search"})
_OBJECT_SEARCH_SIGNATURE = (
    "search(query: str, top_k: int | None) -> dict"
    "  # fields: activated: list, active_count: int, "
    "catalog_version: str, found: bool, matches: list, "
    "message: str, ok: bool"
)
_MIN_OBJECT_FACADE_CHARS = len(f"async self.tools.{_OBJECT_SEARCH_SIGNATURE}")


class EmbeddingError(RuntimeError):
    """The configured embedding service returned an unusable response."""


def is_trivial_chitchat(text: str) -> bool:
    """Return true only for short, plainly social messages."""

    if not text or not text.strip():
        return True
    words = _WORD_RE.findall(text.lower())
    return not words or (len(words) <= 4 and all(word in _TRIVIAL_WORDS for word in words))


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    source = (
        sorted(value, key=lambda item: str(item))
        if isinstance(value, (set, frozenset))
        else value
    )
    items = tuple(
        clean[:512]
        for item in list(source)[:32]
        if isinstance(item, str) and (clean := " ".join(item.split()))
    )
    return items


def _value(value: Any) -> str:
    return str(getattr(value, "value", value or ""))


def _operations(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    operation = properties.get("operation")
    if not isinstance(operation, Mapping):
        return ()
    values = operation.get("enum")
    return _strings(values)


def _argument_terms(schema: Mapping[str, Any]) -> tuple[str, ...]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    result = []
    for raw_name, raw_schema in list(properties.items())[:32]:
        name = " ".join(str(raw_name).split())[:64]
        if not name:
            continue
        description = (
            " ".join(str(raw_schema.get("description", "")).split())[:128]
            if isinstance(raw_schema, Mapping)
            else ""
        )
        result.append(f"{name}: {description}" if description else name)
    return tuple(result)


def _facade_group(capability_id: str) -> str:
    parts = capability_id.split(".")
    head = parts[0]
    if head == "mcp" and len(parts) > 2:
        return _python_identifier(f"mcp_{parts[1]}")
    return _python_identifier(
        _FACADE_GROUPS.get(head, head if len(parts) > 1 else "tools")
    )


def _facade_method(capability_id: str) -> str:
    return _python_identifier(
        capability_id.rsplit(".", 1)[-1]
        if "." in capability_id
        else capability_id
    )


def _python_identifier(value: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    clean = re.sub(r"[^a-z0-9_]", "_", snake.lower())
    clean = clean[:64] if clean and clean[0].isalpha() else f"x_{clean[:62]}"
    return f"{clean[:63]}_" if keyword.iskeyword(clean) else clean


def _python_type(value: Any) -> str:
    return (
        _PYTHON_TYPES.get(str(value.get("type", "")), "Any")
        if isinstance(value, Mapping)
        else "Any"
    )


def _output_contract(value: Any, *, max_chars: int) -> str:
    """Render only bounded field names/types, never the full output schema."""

    prefix = "  # keys: "
    budget = min(
        max_chars,
        len(prefix) + _MAX_FACADE_OUTPUT_CHARS + len(", ..."),
    )
    if not isinstance(value, Mapping) or budget <= 0:
        return ""
    properties = value.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    required = {
        str(name)
        for name in value.get("required", ())
        if isinstance(name, str)
    }
    fields: list[str] = []
    omitted = False
    for name, schema in properties.items():
        name = str(name)
        if not _RESULT_KEY.fullmatch(name):
            omitted = True
            continue
        field = (
            f"{json.dumps(name)}{'' if name in required else '?'}: "
            f"{_python_type(schema)}"
        )
        if len(fields) >= _MAX_FACADE_OUTPUT_FIELDS:
            omitted = True
            break
        fields.append(field)
    if not fields:
        return ""
    while fields:
        contract = (
            prefix
            + ", ".join(fields)
            + (", ..." if omitted else "")
        )
        if len(contract) <= budget:
            return contract
        fields.pop()
        omitted = True
    return ""


def _argument_aliases(
    properties: Mapping[str, Any],
    *,
    hidden: set[str],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for raw_name in properties:
        raw_name = str(raw_name)
        if raw_name in hidden:
            continue
        base = _python_identifier(raw_name)
        alias = base
        nonce = 0
        while alias in aliases:
            material = raw_name if not nonce else f"{raw_name}\0{nonce}"
            suffix = hashlib.sha256(material.encode()).hexdigest()[:8]
            alias = f"{base[:55]}_{suffix}"
            nonce += 1
        aliases[alias] = raw_name
    return aliases


def _allocate_object_methods(
    extension_ids: Iterable[str],
    schemas: "SchemaStore",
    *,
    preferred: Iterable[str] = (),
) -> dict[str, str]:
    priority = {extension_id: index for index, extension_id in enumerate(preferred)}
    buckets: dict[str, list[tuple[str, str]]] = {}
    for capability_id in sorted(set(extension_ids)):
        if not schemas.has(capability_id):
            continue
        schema = schemas.raw(capability_id).get("input_schema")
        operations = _operations(schema) if isinstance(schema, Mapping) else ()
        for raw_method in operations or (_facade_method(capability_id),):
            base = f"{_facade_group(capability_id)}.{_python_identifier(raw_method)}"
            buckets.setdefault(base, []).append((capability_id, raw_method))
    result: dict[str, str] = {}
    used = set(buckets) | _RESERVED_OBJECT_METHODS
    for base in sorted(buckets):
        candidates = buckets[base]
        group, method = base.split(".", 1)
        ordered = sorted(
            candidates,
            key=lambda item: (priority.get(item[0], len(priority)), item),
        )
        for index, (capability_id, raw_method) in enumerate(ordered):
            alias = base
            if index or base in _RESERVED_OBJECT_METHODS:
                nonce = 0
                while True:
                    material = f"{capability_id}\0{raw_method}"
                    if nonce:
                        material += f"\0{nonce}"
                    suffix = hashlib.sha256(material.encode()).hexdigest()[:8]
                    alias = f"{group}.{method[:55]}_{suffix}"
                    if alias not in used:
                        break
                    nonce += 1
                used.add(alias)
            result[f"{capability_id}\0{raw_method}"] = alias
    return result


def _model_name(extension_id: str, used: set[str]) -> str:
    base = _SAFE_TOOL_NAME.sub("_", extension_id).strip("_") or "tool"
    base = base[:64]
    if base not in used:
        return base
    suffix = hashlib.sha256(extension_id.encode("utf-8")).hexdigest()[:8]
    return f"{base[:55]}_{suffix}"


@dataclass(frozen=True, slots=True)
class ToolRecord:
    """Compact routing metadata. Full JSON Schema deliberately lives elsewhere."""

    id: str
    model_name: str
    title: str
    summary: str
    routing_text: str
    domains: tuple[str, ...]
    tags: tuple[str, ...]
    operations: tuple[str, ...]
    anti_examples: tuple[str, ...]
    channels: tuple[str, ...]
    always_available: bool
    permission_level: str
    required_scopes: tuple[str, ...]
    risk_level: str
    side_effects: tuple[str, ...]
    cost_hint: str
    version: str
    schema_hash: str
    routing_hash: str


class SchemaStore:
    """Full tool schemas, keyed by stable host IDs."""

    def __init__(self) -> None:
        self._specs: dict[str, dict[str, Any]] = {}
        self._names: dict[str, str] = {
            TOOL_SEARCH_ID: "tool_search",
            TOOL_CALL_ID: "tool_call",
            OBJECT_RUN_ID: "object_run",
        }

    def sync(self, tools: Iterable[tuple[str, Any]]) -> None:
        used = set(self._names.values())
        current: dict[str, dict[str, Any]] = {}
        for extension_id, item in tools:
            if extension_id not in self._names:
                self._names[extension_id] = _model_name(extension_id, used)
                used.add(self._names[extension_id])
            current[extension_id] = {
                "id": extension_id,
                "model_name": self._names[extension_id],
                "description": str(getattr(item, "description", "") or extension_id),
                "input_schema": dict(getattr(item, "input_schema", {}) or {}),
                "output_schema": dict(getattr(item, "output_schema", {}) or {}),
            }
        current[TOOL_SEARCH_ID] = dict(_SEARCH_SPEC)
        current[TOOL_CALL_ID] = dict(_CALL_SPEC)
        current[OBJECT_RUN_ID] = dict(_OBJECT_RUN_SPEC)
        self._specs = current

    def raw(self, extension_id: str) -> dict[str, Any]:
        return self._specs[extension_id]

    def all_raw(self) -> list[dict[str, Any]]:
        return [dict(spec) for spec in self._specs.values()]

    def openai(self, extension_id: str) -> dict[str, Any]:
        spec = self._specs[extension_id]
        return {
            "type": "function",
            "function": {
                "name": spec["model_name"],
                "description": spec["description"],
                "parameters": spec["input_schema"]
                or {"type": "object", "properties": {}},
            },
        }

    def size(self, extension_id: str) -> int:
        return len(
            json.dumps(
                self.openai(extension_id),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def has(self, extension_id: str) -> bool:
        return extension_id in self._specs


class ToolCatalog:
    """Routing records derived only from tools that actually loaded."""

    def __init__(self) -> None:
        self._records: dict[str, ToolRecord] = {}
        self.version = _json_hash([])

    def sync(
        self,
        tools: Iterable[tuple[str, Any]],
        schemas: SchemaStore,
        manifests: Mapping[str, Any] | None = None,
    ) -> None:
        records: dict[str, ToolRecord] = {}
        manifests = manifests or {}
        for extension_id, item in tools:
            manifest = manifests.get(extension_id)
            if manifest is None:
                manifest = getattr(item, "__corax_manifest__", None)
            if manifest is None:
                manifest = getattr(type(item), "__extension_manifest__", None)
            routing = getattr(manifest, "routing", None)
            if not isinstance(routing, dict):
                routing = getattr(item, "routing", {})
            if not isinstance(routing, dict):
                routing = {}

            schema = schemas.raw(extension_id)
            input_schema = schema["input_schema"]
            title = str(
                routing.get("title") or getattr(item, "name", "") or extension_id
            )[:128]
            summary = str(
                routing.get("summary")
                or getattr(item, "description", "")
                or extension_id
            )[:512]
            intents = _strings(routing.get("intents"))
            examples = _strings(routing.get("examples"))
            anti_examples = _strings(routing.get("anti_examples"))
            domains = _strings(routing.get("domains"))
            tags = _strings(routing.get("tags")) or _strings(
                getattr(item, "tags", ())
            )
            operations = _strings(routing.get("operations")) or _operations(
                input_schema
            )
            arguments = _argument_terms(input_schema)
            channels = _strings(routing.get("channels"))
            permission_level = _value(getattr(item, "permission_level", ""))
            required_scopes = _strings(getattr(item, "required_scopes", ()))
            risk_level = _value(getattr(item, "risk_level", ""))
            side_effects = tuple(
                sorted(_value(effect) for effect in getattr(item, "side_effects", ()))
            )
            cost_hint = str(routing.get("cost") or "")[:128]
            routing_text = "\n".join(
                (
                    f"Tool: {extension_id}",
                    f"Namespace: {_facade_group(extension_id)}",
                    f"Title: {title}",
                    f"Summary: {summary}",
                    f"Domains: {', '.join(domains)}",
                    f"Tags: {', '.join(tags)}",
                    f"Intents: {'; '.join(intents)}",
                    f"Examples: {'; '.join(examples)}",
                    f"Operations: {', '.join(operations)}",
                    f"Arguments: {'; '.join(arguments)}",
                )
            )
            schema_hash = _json_hash(input_schema)
            routing_hash = _json_hash(
                {
                    "text": routing_text,
                    "anti_examples": anti_examples,
                    "channels": channels,
                    "always_available": bool(routing.get("always_available", False)),
                }
            )
            records[extension_id] = ToolRecord(
                id=extension_id,
                model_name=schema["model_name"],
                title=title,
                summary=summary,
                routing_text=routing_text,
                domains=domains,
                tags=tags,
                operations=operations,
                anti_examples=anti_examples,
                channels=channels,
                always_available=bool(routing.get("always_available", False)),
                permission_level=permission_level,
                required_scopes=required_scopes,
                risk_level=risk_level,
                side_effects=side_effects,
                cost_hint=cost_hint,
                version=str(getattr(item, "version", "")),
                schema_hash=schema_hash,
                routing_hash=routing_hash,
            )
        self._records = records
        self.version = _json_hash(
            [
                (record.id, record.schema_hash, record.routing_hash, record.version)
                for record in sorted(records.values(), key=lambda item: item.id)
            ]
        )

    def get(self, extension_id: str) -> ToolRecord:
        return self._records[extension_id]

    def visible(self, channel: str, policy: Any = None) -> list[ToolRecord]:
        denied_ids = set(getattr(policy, "deny_capabilities", ()) or ())
        denied_scopes = set(getattr(policy, "deny_scopes", ()) or ())
        denied_effects = {
            _value(effect)
            for effect in (getattr(policy, "deny_effects", ()) or ())
        }
        result = []
        for record in self._records.values():
            if record.permission_level == "blocked" or record.id in denied_ids:
                continue
            if record.channels and channel not in record.channels:
                continue
            item = record
            if denied_effects.intersection(item.side_effects):
                continue
            if denied_scopes.intersection(record.required_scopes):
                continue
            result.append(record)
        return result

    def __len__(self) -> int:
        return len(self._records)


class OpenAIEmbeddingClient:
    """Tiny OpenAI-compatible embeddings client using only the stdlib."""

    def __init__(self, config: ToolRoutingConfig) -> None:
        self.base_url = os.getenv(
            "CORAX_EMBEDDING_BASE_URL",
            config.base_url,
        ).rstrip("/")
        self.model = os.getenv("CORAX_EMBEDDING_MODEL", config.model)
        self.dimension = config.dimension
        self.timeout = config.timeout_seconds

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: str,
    ) -> list[tuple[float, ...]]:
        if not texts:
            return []
        prefix = "query: " if input_type == "query" else "passage: "
        payload = {
            "model": self.model,
            "input": [prefix + text for text in texts],
            "encoding_format": "float",
        }
        return await asyncio.to_thread(self._post, payload, len(texts))

    def _post(self, payload: dict[str, Any], expected: int) -> list[tuple[float, ...]]:
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - endpoint details stay out of logs
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != expected:
            raise EmbeddingError("embedding response count mismatch")
        rows.sort(key=lambda row: row.get("index", -1) if isinstance(row, dict) else -1)
        vectors: list[tuple[float, ...]] = []
        for row in rows:
            raw = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(raw, list) or len(raw) != self.dimension:
                raise EmbeddingError("embedding response dimension mismatch")
            vector = tuple(float(value) for value in raw)
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingError("embedding response contains non-finite values")
            vectors.append(vector)
        return vectors


class EmbeddingToolRouter:
    """Rank compact ToolRecords with embeddings; never calls a generation LLM."""

    def __init__(
        self,
        config: ToolRoutingConfig,
        *,
        client: OpenAIEmbeddingClient | Any | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.client = client or OpenAIEmbeddingClient(config)
        self.log = log or logging.getLogger("corax.tool_router")
        # ponytail: linear in-memory scan; add persistent ANN only after the
        # measured catalog size makes one query per turn too slow.
        self._vectors: dict[str, tuple[float, ...]] = {}
        self._index_lock = asyncio.Lock()
        self.last_route: dict[str, Any] = {}

    async def rank(
        self,
        query: str,
        records: Sequence[ToolRecord],
        *,
        top_k: int,
        min_similarity: float | None = None,
        explicit: bool = False,
    ) -> list[tuple[ToolRecord, float]]:
        started = time.monotonic()
        if not explicit and is_trivial_chitchat(query):
            self.last_route = {
                "fallback": "trivial",
                "candidates": len(records),
                "selected": 0,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
            return []
        limit = max(1, top_k)
        try:
            await self._ensure_index(records)
            query_vector = (
                await self.client.embed([query], input_type="query")
            )[0]
            ranked = self._embedding_rank(
                query_vector,
                records,
                min_similarity=(
                    self.config.min_similarity
                    if min_similarity is None
                    else min_similarity
                ),
            )[:limit]
            fallback = ""
            if not ranked:
                lexical = self._lexical_rank(query, records)
                lexical_ids = {record.id for record, _ in lexical}
                ranked = [
                    item
                    for item in self._embedding_rank(
                        query_vector,
                        records,
                        min_similarity=0.0,
                    )
                    if item[1] > 0 and item[0].id in lexical_ids
                ][:limit]
                fallback = "lexical" if ranked else ""
        except Exception as exc:  # noqa: BLE001 - routing is fail-closed
            self.log.warning(
                "embedding tool routing unavailable (%s); using lexical fallback",
                type(exc).__name__,
            )
            ranked = self._lexical_rank(query, records)[:limit]
            fallback = type(exc).__name__
        self.last_route = {
            "fallback": fallback,
            "candidates": len(records),
            "selected": len(ranked),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "scores": [
                {"id": record.id, "score": round(score, 4)}
                for record, score in ranked
            ],
        }
        return ranked

    async def _ensure_index(self, records: Sequence[ToolRecord]) -> None:
        missing: list[tuple[str, str]] = []
        for record in records:
            key = f"positive:{record.routing_hash}"
            if key not in self._vectors:
                missing.append((key, record.routing_text))
            for anti in record.anti_examples:
                anti_key = f"anti:{_json_hash(anti)}"
                if anti_key not in self._vectors:
                    missing.append((anti_key, anti))
        if not missing:
            return
        async with self._index_lock:
            pending = [(key, text) for key, text in missing if key not in self._vectors]
            if not pending:
                return
            vectors = await self.client.embed(
                [text for _, text in pending],
                input_type="document",
            )
            self._vectors.update(
                (key, vector)
                for (key, _), vector in zip(pending, vectors, strict=True)
            )

    def _embedding_rank(
        self,
        query: tuple[float, ...],
        records: Sequence[ToolRecord],
        *,
        min_similarity: float,
    ) -> list[tuple[ToolRecord, float]]:
        ranked: list[tuple[ToolRecord, float]] = []
        for record in records:
            positive = _cosine(
                query,
                self._vectors[f"positive:{record.routing_hash}"],
            )
            if positive < min_similarity:
                continue
            anti = max(
                (
                    _cosine(query, self._vectors[f"anti:{_json_hash(example)}"])
                    for example in record.anti_examples
                ),
                default=-1.0,
            )
            if anti >= positive:
                continue
            ranked.append((record, positive))
        ranked.sort(key=lambda item: (-item[1], item[0].id))
        return ranked

    @staticmethod
    def _lexical_rank(
        query: str,
        records: Sequence[ToolRecord],
    ) -> list[tuple[ToolRecord, float]]:
        terms = set(_WORD_RE.findall(query.lower()))
        if not terms:
            return []
        ranked = []
        for record in records:
            positive_terms = set(_WORD_RE.findall(record.routing_text.lower()))
            score = len(terms & positive_terms) / len(terms)
            anti_score = max(
                (
                    len(terms & set(_WORD_RE.findall(example.lower()))) / len(terms)
                    for example in record.anti_examples
                ),
                default=0.0,
            )
            if score > 0 and anti_score < score:
                ranked.append((record, score))
        ranked.sort(key=lambda item: (-item[1], item[0].id))
        return ranked


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingError("embedding dimensions differ")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


@dataclass(slots=True)
class TurnToolSet:
    """Monotonic active set for one user turn."""

    turn_id: str
    session_id: str
    channel: str
    catalog_version: str
    active_ids: list[str] = field(default_factory=list)
    schema_bytes: int = 0
    object_methods: dict[str, str] = field(default_factory=dict)
    exposed_object_facade: dict[str, list[str]] = field(default_factory=dict)
    exposed_object_methods: dict[str, dict[str, Any]] = field(default_factory=dict)
    object_facade_max_chars: int | None = None

    def activate(
        self,
        extension_ids: Iterable[str],
        schemas: SchemaStore,
        *,
        max_tools: int,
        max_schema_bytes: int,
    ) -> list[str]:
        added = []
        for extension_id in extension_ids:
            if extension_id in self.active_ids or not schemas.has(extension_id):
                continue
            size = schemas.size(extension_id)
            if len(self.active_ids) >= max_tools:
                break
            if self.schema_bytes + size > max_schema_bytes:
                continue
            self.active_ids.append(extension_id)
            self.schema_bytes += size
            added.append(extension_id)
        return added


class ToolRoutingHost:
    """One channel-neutral catalog, schema store and per-turn router."""

    def __init__(
        self,
        config: ToolRoutingConfig,
        *,
        client: OpenAIEmbeddingClient | Any | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.log = log or logging.getLogger("corax.tool_routing")
        self.catalog = ToolCatalog()
        self.schemas = SchemaStore()
        self.router = EmbeddingToolRouter(config, client=client, log=self.log)
        self._turns: dict[tuple[str, str], TurnToolSet] = {}

    def sync(
        self,
        tools: Iterable[tuple[str, Any]],
        *,
        manifests: Mapping[str, Any] | None = None,
    ) -> None:
        pairs = list(tools)
        self.schemas.sync(pairs)
        self.catalog.sync(pairs, self.schemas, manifests)

    def all_specs(self) -> list[dict[str, Any]]:
        return self.schemas.all_raw()

    def model_schemas(self) -> list[dict[str, Any]]:
        """Return the fixed native tool prefix sent to every model request."""

        return [
            self.schemas.openai(TOOL_SEARCH_ID),
            self.schemas.openai(TOOL_CALL_ID),
        ]

    def object_model_schema(self) -> list[dict[str, Any]]:
        return [self.schemas.openai(OBJECT_RUN_ID)]

    @staticmethod
    def object_facade_min_chars() -> int:
        return _MIN_OBJECT_FACADE_CHARS

    async def begin_turn(
        self,
        user_text: str,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
        policy: Any = None,
    ) -> TurnToolSet:
        visible = self.catalog.visible(channel, policy)
        ranked = await self.router.rank(
            user_text,
            visible,
            top_k=self.config.top_k,
        )
        turn = TurnToolSet(
            turn_id=turn_id,
            session_id=session_id,
            channel=channel,
            catalog_version=self.catalog.version,
        )
        turn.activate(
            [TOOL_SEARCH_ID],
            self.schemas,
            max_tools=self.config.max_active_tools,
            max_schema_bytes=self.config.max_schema_bytes,
        )
        turn.activate(
            [record.id for record in visible if record.always_available],
            self.schemas,
            max_tools=self.config.max_active_tools,
            max_schema_bytes=self.config.max_schema_bytes,
        )
        turn.activate(
            [record.id for record, _ in ranked],
            self.schemas,
            max_tools=self.config.max_active_tools,
            max_schema_bytes=self.config.max_schema_bytes,
        )
        turn.object_methods = _allocate_object_methods(
            [record.id for record in visible],
            self.schemas,
            preferred=turn.active_ids,
        )
        self._turns[(channel, session_id)] = turn
        self.log.info(
            "tool routing turn=%s catalog=%s candidates=%d active=%d schemas=%dB "
            "embedding_ms=%s fallback=%s",
            turn_id,
            self.catalog.version[:12],
            len(visible),
            len(turn.active_ids),
            turn.schema_bytes,
            self.router.last_route.get("latency_ms", 0),
            self.router.last_route.get("fallback", ""),
        )
        return turn

    def active_schemas(
        self,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
    ) -> list[dict[str, Any]]:
        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id:
            return []
        return [
            self.schemas.openai(extension_id)
            for extension_id in turn.active_ids
            if self.schemas.has(extension_id)
        ]

    def active_descriptors(
        self,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
    ) -> list[dict[str, Any]]:
        """Return selected schemas as prompt data, not top-level model tools."""

        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id:
            return []
        result = []
        for extension_id in turn.active_ids:
            if extension_id == TOOL_SEARCH_ID or not self.schemas.has(extension_id):
                continue
            spec = self.schemas.raw(extension_id)
            record = self.catalog.get(extension_id)
            result.append(
                {
                    "id": extension_id,
                    "model_name": spec["model_name"],
                    "description": spec["description"],
                    "input_schema": spec["input_schema"],
                    "risk_level": record.risk_level,
                    "required_scopes": list(record.required_scopes),
                    "side_effects": list(record.side_effects),
                }
            )
        return result

    def object_facade(
        self,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
        max_chars: int = _DEFAULT_OBJECT_FACADE_CHARS,
        publish: bool = False,
    ) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
        """Build bounded Python signatures plus a host-only method map."""

        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id:
            return {}, {}
        max_chars = max(0, int(max_chars))
        if publish:
            if turn.object_facade_max_chars is None:
                turn.object_facade_max_chars = max_chars
            max_chars = turn.object_facade_max_chars
        search_signature = _OBJECT_SEARCH_SIGNATURE
        search_rendered = f"async self.tools.{search_signature}"
        if publish and turn.exposed_object_facade:
            facade = {
                group: list(signatures)
                for group, signatures in turn.exposed_object_facade.items()
            }
            methods = {
                key: dict(descriptor)
                for key, descriptor in turn.exposed_object_methods.items()
            }
        else:
            if len(search_rendered) > max_chars:
                if publish:
                    turn.exposed_object_facade = {}
                    turn.exposed_object_methods = {}
                return {}, {}
            facade = {"tools": [search_signature]}
            methods = {
                "tools.search": {
                    "capability": TOOL_SEARCH_ID,
                    "inject": {},
                    "allowed": ["query", "top_k"],
                    "required": ["query"],
                }
            }
        rendered_signatures = [
            f"async self.{group}.{signature}"
            for group, signatures in facade.items()
            for signature in signatures
        ]
        method_count = len(rendered_signatures)
        rendered_chars = len("\n".join(rendered_signatures))
        for capability_id in sorted(turn.active_ids):
            if capability_id == TOOL_SEARCH_ID or not self.schemas.has(capability_id):
                continue
            spec = self.schemas.raw(capability_id)
            schema = spec.get("input_schema")
            properties = (
                schema.get("properties", {})
                if isinstance(schema, Mapping)
                else {}
            )
            if not isinstance(properties, Mapping):
                properties = {}
            required = {
                str(name)
                for name in (
                    schema.get("required", ())
                    if isinstance(schema, Mapping)
                    else ()
                )
            }
            operations = _operations(schema) if isinstance(schema, Mapping) else ()
            group = _facade_group(capability_id)
            method_names = operations or (_facade_method(capability_id),)
            for raw_method in method_names:
                key = turn.object_methods.get(f"{capability_id}\0{raw_method}")
                if not key or key in methods:
                    continue
                group, method = key.split(".", 1)
                if not _PYTHON_NAME.fullmatch(group) or not _PYTHON_NAME.fullmatch(method):
                    continue
                inject = {"operation": raw_method} if operations else {}
                hidden = {"state_key"}
                if operations:
                    hidden.add("operation")
                arguments = _argument_aliases(properties, hidden=hidden)
                required_args = [
                    alias
                    for alias, raw_name in arguments.items()
                    if raw_name in required
                ]
                if required - hidden - set(arguments.values()):
                    continue
                optional_args = [
                    alias
                    for alias in arguments
                    if alias not in required_args
                ]
                signature_parts = [
                    f"{name}: {_python_type(properties.get(arguments[name]))}"
                    for name in required_args
                ]
                signature_parts.extend(
                    f"{name}: {_python_type(properties.get(arguments[name]))} | None"
                    for name in optional_args
                )
                declaration = (
                    f"{method}({', '.join(signature_parts)}) -> dict"
                )
                if len(declaration) > _MAX_OBJECT_SIGNATURE_CHARS:
                    continue
                signature = (
                    declaration
                    + _output_contract(
                        spec.get("output_schema"),
                        max_chars=_MAX_OBJECT_SIGNATURE_CHARS - len(declaration),
                    )
                )
                group_methods = facade.get(group)
                if (
                    method_count >= _MAX_OBJECT_FACADE_METHODS
                    or (
                        group_methods is None
                        and len(facade) >= _MAX_OBJECT_FACADE_GROUPS
                    )
                    or (
                        group_methods is not None
                        and len(group_methods) >= _MAX_OBJECT_FACADE_METHODS_PER_GROUP
                    )
                ):
                    continue
                rendered = f"async self.{group}.{signature}"
                added_chars = len(rendered) + (1 if method_count else 0)
                if rendered_chars + added_chars > max_chars:
                    continue
                facade.setdefault(group, []).append(signature)
                method_count += 1
                rendered_chars += added_chars
                methods[key] = {
                    "capability": capability_id,
                    "inject": inject,
                    "allowed": sorted(arguments),
                    "required": required_args,
                    "arguments": arguments,
                }
        result_facade = {
            group: sorted(signatures)
            for group, signatures in sorted(facade.items())
        }
        if publish:
            turn.exposed_object_facade = result_facade
            turn.exposed_object_methods = methods
        return result_facade, methods

    def object_namespaces(
        self,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
    ) -> list[str]:
        """Return compact discovery hints from this turn's visible facade map."""

        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id:
            return []
        # ponytail: mirror the facade group cap; raise both only when a real
        # catalog needs more than sixteen simultaneous discovery hints.
        return sorted(
            {alias.split(".", 1)[0] for alias in turn.object_methods.values()}
        )[:_MAX_OBJECT_FACADE_GROUPS]

    def resolve_object_method(
        self,
        method: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        turn_id: str,
        channel: str,
    ) -> tuple[str, dict[str, Any]]:
        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id:
            raise KeyError(f"object method is unavailable: {method}")
        descriptor = turn.exposed_object_methods.get(method)
        if descriptor is None:
            raise KeyError(f"object method is unavailable: {method}")
        if not isinstance(arguments, dict):
            raise ValueError("object method arguments must be an object")
        allowed = set(descriptor["allowed"])
        unknown = set(arguments) - allowed
        required = set(descriptor["required"])
        clean_arguments = {
            name: value
            for name, value in arguments.items()
            if value is not None or name in required
        }
        missing = required - set(clean_arguments)
        if unknown:
            raise ValueError(
                "unknown object method arguments: " + ", ".join(sorted(unknown))
            )
        if missing:
            raise ValueError(
                "missing object method arguments: " + ", ".join(sorted(missing))
            )
        return str(descriptor["capability"]), {
            **descriptor["inject"],
            **{
                descriptor.get("arguments", {}).get(name, name): value
                for name, value in clean_arguments.items()
            },
        }

    def current_turn(
        self,
        *,
        session_id: str,
        channel: str,
    ) -> TurnToolSet | None:
        return self._turns.get((channel, session_id))

    def has_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
    ) -> bool:
        turn = self.current_turn(session_id=session_id, channel=channel)
        return turn is not None and turn.turn_id == turn_id

    def has_active_turns(self) -> bool:
        return bool(self._turns)

    def require_active(
        self,
        extension_id: str,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
    ) -> None:
        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id or extension_id not in turn.active_ids:
            raise PermissionError(
                f"tool {extension_id!r} is not active for turn {turn_id!r}"
            )

    async def search(
        self,
        query: str,
        *,
        session_id: str,
        turn_id: str,
        channel: str,
        policy: Any = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        turn = self._turn(session_id=session_id, channel=channel)
        if turn.turn_id != turn_id:
            raise PermissionError("tool.search is not active for this turn")
        query = " ".join(str(query).split())
        if not query:
            raise ValueError("tool.search query must not be empty")
        if len(query) > 1000:
            raise ValueError("tool.search query is too long")
        ranked = await self.router.rank(
            query,
            self.catalog.visible(channel, policy),
            top_k=min(max(int(top_k), 1), 10),
            explicit=True,
        )
        activated = turn.activate(
            [record.id for record, _ in ranked],
            self.schemas,
            max_tools=self.config.max_active_tools,
            max_schema_bytes=self.config.max_schema_bytes,
        )
        turn.catalog_version = self.catalog.version
        found = bool(ranked)
        blocked = [
            record.id for record, _ in ranked if record.id not in turn.active_ids
        ]
        if blocked:
            message = (
                "Some matching tools could not be activated within the turn "
                "limits. Do not call them; use active methods or report the "
                "missing capability."
            )
        elif activated:
            message = (
                "Matching tools were activated for this capability need. "
                "Search again for every other missing capability, then use "
                "the appended schemas."
            )
        elif found:
            message = "Matching tools are already active for this turn."
        else:
            message = (
                "No matching tool is available in this environment. "
                "Tell the user that this capability is unavailable and "
                "do not repeat tool.search with synonyms."
            )
        return {
            "ok": True,
            "found": found,
            "message": message,
            "matches": [
                {
                    "id": record.id,
                    "title": record.title,
                    "summary": record.summary,
                    "score": round(score, 4),
                }
                for record, score in ranked
            ],
            "activated": activated,
            "active_count": len(turn.active_ids),
            "catalog_version": turn.catalog_version,
        }

    def end_turn(self, *, session_id: str, channel: str) -> None:
        self._turns.pop((channel, session_id), None)

    def clear_turns(self) -> None:
        self._turns.clear()

    def _turn(self, *, session_id: str, channel: str) -> TurnToolSet:
        try:
            return self._turns[(channel, session_id)]
        except KeyError:
            raise PermissionError("no active tool set for this turn") from None
