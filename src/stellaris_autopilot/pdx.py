"""Small, lossless-for-structure text Clausewitz parser.

Objects retain repeated keys and anonymous values instead of silently converting
the save format to a JSON object. No third-party parser code is used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterator


class PDXError(ValueError):
    """The input is not supported textual Clausewitz data."""


@dataclass
class PDXNode:
    entries: list[tuple[str | None, Any]] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the last matching key, preserving all matches in get_all()."""
        for entry_key, value in reversed(self.entries):
            if entry_key == str(key):
                return value
        return default

    def get_all(self, key: str) -> list[Any]:
        return [value for entry_key, value in self.entries if entry_key == str(key)]

    def items(self) -> Iterator[tuple[str, Any]]:
        return ((key, value) for key, value in self.entries if key is not None)

    def values(self) -> Iterator[Any]:
        return (value for key, value in self.entries if key is None)


@dataclass(frozen=True)
class _Token:
    text: str
    position: int
    quoted: bool = False


def _tokens(text: str) -> Iterator[_Token]:
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char.isspace() or char == '\ufeff':
            pos += 1
            continue
        if char == '#':
            newline = text.find('\n', pos)
            pos = len(text) if newline < 0 else newline + 1
            continue
        if char in '{}=':
            yield _Token(char, pos)
            pos += 1
            continue
        start = pos
        if char == '"':
            pos += 1
            value: list[str] = []
            while pos < len(text):
                char = text[pos]
                if char == '"':
                    pos += 1
                    yield _Token(''.join(value), start, True)
                    break
                if char == '\\':
                    pos += 1
                    if pos == len(text):
                        raise PDXError(f'Unterminated escape at offset {pos}')
                    escaped = text[pos]
                    value.append({'n': '\n', 'r': '\r', 't': '\t', '"': '"', '\\': '\\'}.get(escaped, '\\' + escaped))
                else:
                    value.append(char)
                pos += 1
            else:
                raise PDXError(f'Unterminated quoted string at offset {start}')
            continue
        while pos < len(text) and not text[pos].isspace() and text[pos] not in '{}=#"':
            pos += 1
        if start == pos:
            raise PDXError(f'Unexpected character at offset {pos}')
        yield _Token(text[start:pos], start)


_NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')


def _scalar(token: _Token) -> Any:
    if not token.quoted and _NUMBER.fullmatch(token.text):
        try:
            return int(token.text) if all(c not in token.text for c in '.eE') else float(token.text)
        except ValueError:
            pass
    return token.text


def loads(text: str) -> PDXNode:
    """Parse text, raising PDXError for malformed input or excessive nesting.

    Keys always remain strings; numeric values become int/float. Dates and the
    PDX yes/no keywords remain strings. Typed color constructs such as ``rgb``
    are retained as separate anonymous values, not interpreted as commands.
    """
    if '\x00' in text:
        raise PDXError('Binary/NUL-containing saves are unsupported; use a text save')
    source = iter(_tokens(text))
    current = next(source, None)

    def take() -> _Token | None:
        nonlocal current
        result = current
        current = next(source, None)
        return result

    def node(depth: int, nested: bool) -> PDXNode:
        if depth > 128:
            raise PDXError('Object nesting exceeds the supported limit (128)')
        result = PDXNode()
        while current is not None:
            if current.text == '}' and not current.quoted:
                if not nested:
                    raise PDXError(f'Unexpected closing brace at offset {current.position}')
                take()
                return result
            if current.text == '=' and not current.quoted:
                raise PDXError(f'Unexpected equals sign at offset {current.position}')
            if current.text == '{' and not current.quoted:
                take()
                result.entries.append((None, node(depth + 1, True)))
                continue
            first = take()
            assert first is not None
            if current is not None and current.text == '=' and not current.quoted:
                take()
                if current is None or (current.text in '}=' and not current.quoted):
                    raise PDXError(f'Missing value for {first.text!r} at offset {first.position}')
                if current.text == '{' and not current.quoted:
                    take()
                    value = node(depth + 1, True)
                else:
                    token = take()
                    assert token is not None
                    value = _scalar(token)
                result.entries.append((first.text, value))
            else:
                result.entries.append((None, _scalar(first)))
        if nested:
            raise PDXError('Unterminated object: missing closing brace')
        return result

    return node(0, False)
