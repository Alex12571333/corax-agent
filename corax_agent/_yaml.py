"""Tiny YAML facade.

We prefer :mod:`PyYAML` when it is installed. When it is not, we fall
back to a *minimal* block-style YAML reader/writer that covers exactly
the subset used by ``agent.yaml`` (nested mappings, scalar sequences,
strings / ints / floats / bools / null, ``#`` comments).

This keeps the scaffold runnable on a pure stdlib install while still
storing config as YAML. If you need full YAML, just ``pip install
pyyaml`` and this module transparently uses it.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised indirectly depending on environment
    import yaml as _pyyaml

    HAS_PYYAML = True
except Exception:  # pragma: no cover
    _pyyaml = None
    HAS_PYYAML = False


PYYAML_HINT = "PyYAML is required to read agent.yaml. Install with: pip install pyyaml"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def loads(text: str) -> Any:
    """Parse a YAML document into Python objects."""
    if HAS_PYYAML:
        return _pyyaml.safe_load(text) or {}
    return _fallback_loads(text)


def dumps(obj: Any) -> str:
    """Serialise Python objects into a YAML document."""
    if HAS_PYYAML:
        return _pyyaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
    return _fallback_dumps(obj)


# --------------------------------------------------------------------------- #
# Fallback writer
# --------------------------------------------------------------------------- #
def _fallback_dumps(obj: Any) -> str:
    lines: list[str] = []
    _dump_node(obj, 0, lines)
    return "\n".join(lines) + "\n"


def _dump_node(obj: Any, indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict):
                if value:
                    lines.append(f"{pad}{key}:")
                    _dump_node(value, indent + 1, lines)
                else:
                    lines.append(f"{pad}{key}: {{}}")
            elif isinstance(value, list):
                if value:
                    lines.append(f"{pad}{key}:")
                    item_pad = "  " * (indent + 1)
                    for item in value:
                        lines.append(f"{item_pad}- {_scalar_to_str(item)}")
                else:
                    lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_scalar_to_str(value)}")
    else:  # top-level scalar / list — uncommon for us
        lines.append(f"{pad}{_scalar_to_str(obj)}")


_QUOTE_TRIGGERS = set("!&*?|>%@`\"'#,[]{}~ ")


def _scalar_to_str(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    needs_quote = (
        text == ""
        or text.strip() != text
        or text[0] in _QUOTE_TRIGGERS
        or ":" in text
        or text.lower() in ("true", "false", "null", "yes", "no", "on", "off")
    )
    if needs_quote:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


# --------------------------------------------------------------------------- #
# Fallback reader
# --------------------------------------------------------------------------- #
def _fallback_loads(text: str) -> Any:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    value, _ = _parse_block(tokens, 0, tokens[0][0])
    return value if value is not None else {}


def _tokenize(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        lead = raw.lstrip(" ")
        if lead.startswith("#"):
            continue
        indent = len(raw) - len(lead)
        out.append((indent, lead.rstrip()))
    return out


def _parse_block(lines: list[tuple[int, str]], idx: int, indent: int):
    _, content = lines[idx]
    if content == "-" or content.startswith("- "):
        return _parse_seq(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_map(lines: list[tuple[int, str]], idx: int, indent: int):
    result: dict[str, Any] = {}
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent != indent:
            break
        if content == "-" or content.startswith("- "):
            break  # a sequence at this level belongs to the previous key
        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        idx += 1
        if rest == "":
            if idx < len(lines):
                nxt_indent, nxt_content = lines[idx]
                is_seq = nxt_content == "-" or nxt_content.startswith("- ")
                if is_seq and nxt_indent >= indent:
                    result[key], idx = _parse_seq(lines, idx, nxt_indent)
                elif nxt_indent > indent:
                    result[key], idx = _parse_block(lines, idx, nxt_indent)
                else:
                    result[key] = None
            else:
                result[key] = None
        else:
            result[key] = _parse_scalar(rest)
    return result, idx


def _parse_seq(lines: list[tuple[int, str]], idx: int, indent: int):
    result: list[Any] = []
    while idx < len(lines):
        cur_indent, content = lines[idx]
        is_seq = content == "-" or content.startswith("- ")
        if cur_indent != indent or not is_seq:
            break
        item = content[1:].strip()
        idx += 1
        if item == "" and idx < len(lines) and lines[idx][0] > indent:
            result_item, idx = _parse_block(lines, idx, lines[idx][0])
            result.append(result_item)
        else:
            result.append(_parse_scalar(item))
    return result, idx


def _parse_scalar(text: str) -> Any:
    if text and text[0] not in "\"'":
        hash_pos = text.find(" #")
        if hash_pos != -1:
            text = text[:hash_pos].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    if text in ("{}",):
        return {}
    if text in ("[]",):
        return []
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text
