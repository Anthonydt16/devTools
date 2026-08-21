"""A tiny strict reader for YAML-derived documents.

Why not Pydantic?  Three reasons, in order of weight:

1. **Error messages.** Devbox promises "erreurs détectées avant tout démarrage"
   with an exact location.  A hand-rolled reader emits
   ``services.backend.healthcheck.http.timeout: expected a number, got 'abc'``
   and collects *every* problem in one pass instead of aborting on the first.
2. **Unknown keys.** A typo such as ``depend_on`` must fail loudly.  That is one
   line here and a per-model configuration there.
3. **Packaging.** ``devbox.exe`` is built with PyInstaller; Pydantic's compiled
   core adds weight and hidden imports for validation we already need to
   hand-write to satisfy point 1.

The reader never raises on a bad field: it records a
:class:`~devbox.errors.ValidationIssue` and returns a safe default so the rest of
the document is still checked.  :meth:`Reader.raise_if_invalid` raises once, at
the end, with the full list.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Final, TypeVar

from devbox.errors import ConfigError, ValidationIssue

__all__ = ["Node", "Reader"]

_T = TypeVar("_T")

_SCALAR_NAMES: Final = {
    str: "a string",
    int: "an integer",
    float: "a number",
    bool: "a boolean",
}


def _type_name(value: object) -> str:
    match value:
        case bool():
            return "a boolean"
        case int():
            return "an integer"
        case float():
            return "a number"
        case str():
            return "a string"
        case Mapping():
            return "a mapping"
        case list() | tuple():
            return "a list"
        case None:
            return "null"
        case _:
            return type(value).__name__


class Reader:
    """Accumulates validation issues across a whole document."""

    __slots__ = ("issues", "source")

    def __init__(self, source: str | None = None) -> None:
        self.issues: list[ValidationIssue] = []
        self.source = source

    def error(self, path: str, message: str) -> None:
        self.issues.append(ValidationIssue(path=path, message=message))

    @property
    def ok(self) -> bool:
        return not self.issues

    def raise_if_invalid(self, message: str, *, hint: str | None = None) -> None:
        if self.issues:
            raise ConfigError(
                message,
                issues=sorted(self.issues, key=lambda issue: issue.path),
                source=self.source,
                hint=hint,
            )

    def root(self, value: object, path: str = "") -> Node:
        return Node(value=value, path=path, reader=self)


class Node:
    """One position in the document, with its dotted path for error reporting."""

    __slots__ = ("_reader", "path", "value")

    def __init__(self, value: object, path: str, reader: Reader) -> None:
        self.value = value
        self.path = path
        self._reader = reader

    # -- issue reporting ------------------------------------------------------
    @property
    def reader(self) -> Reader:
        return self._reader

    def error(self, message: str) -> None:
        """Record an issue located at this node."""
        self._reader.error(self.path or "<root>", message)

    def error_at(self, key: str, message: str) -> None:
        """Record an issue located at a child of this node."""
        self._reader.error(self._child_path(key), message)

    # -- structure ------------------------------------------------------------
    def _child_path(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def is_mapping(self) -> bool:
        return isinstance(self.value, Mapping)

    def is_missing(self) -> bool:
        return self.value is None

    def as_mapping(self) -> Mapping[str, object]:
        """Return this node as a mapping, recording an issue if it is not one."""
        if isinstance(self.value, Mapping):
            bad_keys = [key for key in self.value if not isinstance(key, str)]
            if bad_keys:
                self._reader.error(
                    self.path or "<root>",
                    f"keys must be strings, got {bad_keys[0]!r}",
                )
            return {str(k): v for k, v in self.value.items()}
        if self.value is not None:
            self._reader.error(self.path or "<root>", f"expected a mapping, got {_type_name(self.value)}")
        return {}

    def child(self, key: str) -> Node:
        mapping = self.as_mapping()
        return Node(value=mapping.get(key), path=self._child_path(key), reader=self._reader)

    def children(self) -> Iterator[tuple[str, Node]]:
        for key, value in self.as_mapping().items():
            yield key, Node(value=value, path=self._child_path(key), reader=self._reader)

    def reject_unknown_keys(self, allowed: Sequence[str]) -> None:
        """Fail on any key outside *allowed*, suggesting the closest match."""
        allowed_set = set(allowed)
        for key in self.as_mapping():
            if key in allowed_set:
                continue
            suggestion = _closest(key, allowed)
            hint = f", did you mean {suggestion!r}?" if suggestion else ""
            self._reader.error(
                self._child_path(key),
                f"unknown key {key!r}{hint} (allowed: {', '.join(sorted(allowed_set))})",
            )

    def exactly_one_of(self, keys: Sequence[str]) -> str | None:
        """Return the single present key among *keys*, or record an issue."""
        present = [key for key in keys if self.as_mapping().get(key) is not None]
        if len(present) == 1:
            return present[0]
        options = ", ".join(keys)
        if not present:
            self._reader.error(self.path, f"exactly one of [{options}] is required, got none")
        else:
            self._reader.error(
                self.path,
                f"exactly one of [{options}] is allowed, got {len(present)}: {', '.join(present)}",
            )
        return None

    # -- scalars --------------------------------------------------------------
    def _scalar(self, expected: type[_T], default: _T, *, required: bool) -> _T:
        value = self.value
        if value is None:
            if required:
                self._reader.error(self.path, "is required")
            return default
        # bool is a subclass of int: never silently accept True as 1.
        if expected is not bool and isinstance(value, bool):
            self._reader.error(self.path, f"expected {_SCALAR_NAMES[expected]}, got a boolean")
            return default
        if expected is float and isinstance(value, int):
            return expected(value)  # type: ignore[return-value]
        if isinstance(value, expected):
            return value
        self._reader.error(
            self.path,
            f"expected {_SCALAR_NAMES.get(expected, expected.__name__)}, "
            f"got {_type_name(value)} ({value!r})",
        )
        return default

    def require_str(self, *, allow_empty: bool = False) -> str:
        value = self._scalar(str, "", required=True)
        # Only complain about emptiness when a *string* was actually given;
        # a type error above already explains the problem.
        if value == "" and not allow_empty and isinstance(self.value, str):
            self._reader.error(self.path, "must not be empty")
        return value

    def optional_str(self, default: str | None = None) -> str | None:
        if self.value is None:
            return default
        return self._scalar(str, default or "", required=False)

    def scalar_str(self, default: str) -> str:
        """Read a string, coercing YAML numbers so ``version: 17`` works."""
        if self.value is None:
            return default
        if isinstance(self.value, bool):
            self._reader.error(self.path, "expected a string, got a boolean")
            return default
        if isinstance(self.value, int | float):
            return str(self.value)
        return self._scalar(str, default, required=False)

    def require_int(self) -> int:
        return self._scalar(int, 0, required=True)

    def optional_int(self, default: int | None = None) -> int | None:
        if self.value is None:
            return default
        return self._scalar(int, default or 0, required=False)

    def optional_float(self, default: float) -> float:
        if self.value is None:
            return default
        return self._scalar(float, default, required=False)

    def optional_bool(self, default: bool) -> bool:  # noqa: FBT001 - mirrors YAML shape
        if self.value is None:
            return default
        return self._scalar(bool, default, required=False)

    def port(self, *, required: bool = False) -> int | None:
        if self.value is None:
            if required:
                self._reader.error(self.path, "is required")
            return None
        value = self._scalar(int, 0, required=required)
        if value and not (1 <= value <= 65535):
            self._reader.error(self.path, f"port must be between 1 and 65535, got {value}")
            return None
        return value or None

    def positive_number(self, default: float, *, allow_zero: bool = True) -> float:
        value = self.optional_float(default)
        floor = 0.0 if allow_zero else 0.0001
        if value < floor:
            comparison = "negative" if allow_zero else "zero or negative"
            self._reader.error(self.path, f"must not be {comparison}, got {value}")
            return default
        return value

    # -- collections ----------------------------------------------------------
    def string_list(self) -> tuple[str, ...]:
        if self.value is None:
            return ()
        if isinstance(self.value, str):
            self._reader.error(self.path, "expected a list of strings, got a string")
            return ()
        if not isinstance(self.value, list | tuple):
            self._reader.error(self.path, f"expected a list, got {_type_name(self.value)}")
            return ()
        out: list[str] = []
        for index, item in enumerate(self.value):
            if isinstance(item, str):
                out.append(item)
            else:
                self._reader.error(f"{self.path}[{index}]", f"expected a string, got {_type_name(item)}")
        return tuple(out)

    def int_list(self) -> tuple[int, ...]:
        if self.value is None:
            return ()
        if not isinstance(self.value, list | tuple):
            self._reader.error(self.path, f"expected a list, got {_type_name(self.value)}")
            return ()
        out: list[int] = []
        for index, item in enumerate(self.value):
            if isinstance(item, bool) or not isinstance(item, int):
                self._reader.error(f"{self.path}[{index}]", f"expected an integer, got {_type_name(item)}")
            else:
                out.append(item)
        return tuple(out)

    def string_mapping(self) -> dict[str, str]:
        """A ``key: value`` block where values are coerced to strings.

        YAML turns ``PORT: 3000`` into an ``int``; environment variables are
        strings, so numbers and booleans are stringified rather than rejected
        (``true`` becomes ``"true"``, matching what a shell would see).
        """
        if self.value is None:
            return {}
        out: dict[str, str] = {}
        for key, child in self.children():
            value = child.value
            match value:
                case str():
                    out[key] = value
                case bool():
                    out[key] = "true" if value else "false"
                case int() | float():
                    out[key] = str(value)
                case None:
                    out[key] = ""
                case _:
                    self._reader.error(
                        child.path,
                        f"expected a string, got {_type_name(value)}",
                    )
        return out

    def enum(self, values: Sequence[str], default: str) -> str:
        raw = self.optional_str(default)
        if raw is None:
            return default
        if raw not in values:
            self._reader.error(
                self.path,
                f"expected one of [{', '.join(values)}], got {raw!r}",
            )
            return default
        return raw


def _closest(word: str, candidates: Sequence[str]) -> str | None:
    """Cheap typo suggestion — good enough for one-character slips."""
    import difflib

    matches = difflib.get_close_matches(word, list(candidates), n=1, cutoff=0.7)
    return matches[0] if matches else None
