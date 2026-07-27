"""Host-owned tool catalog and embedding-only per-turn selection."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
_SAFE_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]")
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
        "Find and activate additional tools for the current turn. "
        "Use when the visible tools are insufficient."
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
    items = tuple(
        clean
        for item in value
        if isinstance(item, str) and (clean := " ".join(item.split()))
    )
    return tuple(sorted(items)) if isinstance(value, (set, frozenset)) else items


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
            }
        current[TOOL_SEARCH_ID] = dict(_SEARCH_SPEC)
        current[TOOL_CALL_ID] = dict(_CALL_SPEC)
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
            title = str(routing.get("title") or getattr(item, "name", "") or extension_id)
            summary = str(
                routing.get("summary")
                or getattr(item, "description", "")
                or extension_id
            )
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
            channels = _strings(routing.get("channels"))
            permission_level = _value(getattr(item, "permission_level", ""))
            required_scopes = _strings(getattr(item, "required_scopes", ()))
            risk_level = _value(getattr(item, "risk_level", ""))
            side_effects = tuple(
                sorted(_value(effect) for effect in getattr(item, "side_effects", ()))
            )
            cost_hint = str(routing.get("cost") or "")
            routing_text = "\n".join(
                (
                    f"Tool: {extension_id}",
                    f"Title: {title}",
                    f"Summary: {summary}",
                    f"Domains: {', '.join(domains)}",
                    f"Tags: {', '.join(tags)}",
                    f"Intents: {'; '.join(intents)}",
                    f"Examples: {'; '.join(examples)}",
                    f"Operations: {', '.join(operations)}",
                    f"Permission: {permission_level}",
                    f"Risk: {risk_level}",
                    f"Side effects: {', '.join(side_effects)}",
                    f"Required scopes: {', '.join(required_scopes)}",
                    f"Channels: {', '.join(channels)}",
                    f"Cost: {cost_hint}",
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
            )[: max(1, top_k)]
            fallback = ""
        except Exception as exc:  # noqa: BLE001 - routing is fail-closed
            self.log.warning(
                "embedding tool routing unavailable (%s); using lexical fallback",
                type(exc).__name__,
            )
            ranked = self._lexical_rank(query, records)[: max(1, top_k)]
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
            min_similarity=0.0,
            explicit=True,
        )
        activated = turn.activate(
            [record.id for record, _ in ranked],
            self.schemas,
            max_tools=self.config.max_active_tools,
            max_schema_bytes=self.config.max_schema_bytes,
        )
        turn.catalog_version = self.catalog.version
        return {
            "ok": True,
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
            "tools": [
                descriptor
                for descriptor in self.active_descriptors(
                    session_id=session_id,
                    turn_id=turn_id,
                    channel=channel,
                )
                if descriptor["id"] in {record.id for record, _ in ranked}
            ],
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
