"""Thread-safe tool registry: one metadata source, one dispatch path.

Metadata shape::

    [{"name": str, "description": str,
      "params": [{"name": str, "type": str, "required": bool, "default": ...}],
      "example": dict}]

``default`` is present only when the tool declared one. ``list_metadata()``
returns a deep copy. Handlers are ``handler(context, **validated_arguments)``.
``dispatch`` is synchronous, does not swallow exceptions, and re-raises
:class:`~dbtools.errors.ToolError` unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import BadParamError
ParamType = Literal["string", "int", "number", "bool", "list", "dict"]
_PARAM_TYPES = frozenset({"string", "int", "number", "bool", "list", "dict"})
_MISSING = object()


@dataclass(frozen=True)
class ParamSpec:
    """Single tool parameter. ``default`` is omitted from metadata when unset."""

    name: str
    type: str
    required: bool = True
    default: Any = field(default=_MISSING)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or self.name != self.name.strip():
            raise ValueError(f"invalid parameter name: {self.name!r}")
        if self.name == "context":
            raise ValueError("parameter name 'context' collides with handler(context, **kwargs)")
        if self.type not in _PARAM_TYPES:
            raise ValueError(f"unsupported parameter type {self.type!r} for {self.name}")
        if self.required and self.has_default():
            raise ValueError(f"required parameter {self.name!r} cannot declare a default")

    def has_default(self) -> bool:
        return self.default is not _MISSING

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required": bool(self.required),
        }
        if self.has_default():
            payload["default"] = copy.deepcopy(self.default)
        return payload


@dataclass(frozen=True)
class ToolDefinition:
    """One registered tool. Description, params, and example are single-source."""

    name: str
    description: str
    params: tuple[ParamSpec, ...]
    example: dict[str, Any]
    handler: Callable[..., Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip() or self.name != self.name.strip():
            raise ValueError(f"invalid tool name: {self.name!r}")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError(f"tool {self.name!r} needs a non-empty description")
        if not callable(self.handler):
            raise ValueError(f"tool {self.name!r} handler must be callable")
        if not isinstance(self.example, dict):
            raise ValueError(f"tool {self.name!r} example must be a dict")
        params = tuple(self.params)
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "example", copy.deepcopy(self.example))
        seen: set[str] = set()
        for spec in params:
            if not isinstance(spec, ParamSpec):
                raise ValueError(f"tool {self.name!r} params must be ParamSpec instances")
            if spec.name in seen:
                raise ValueError(f"tool {self.name!r} duplicates parameter {spec.name!r}")
            seen.add(spec.name)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "params": [spec.to_metadata() for spec in self.params],
            "example": copy.deepcopy(self.example),
        }


class ToolRegistry:
    """Insertion-ordered registry used for ``tools/list`` and dispatch.

    락이 없다. ``ToolService`` 가 ``_build_registry()`` 로 완성된 인스턴스를
    만든 뒤 자기 락 아래에서 참조만 교체하므로, 여기 담긴 dict 는 공개된
    시점부터 불변이다. 읽기 전용 dict 접근은 그 자체로 안전하다.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """Add one tool. 공개 전(빌드 중)에만 호출된다."""
        if not isinstance(tool, ToolDefinition):
            raise TypeError("register() requires a ToolDefinition")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        """Look up one tool, or ``None`` when it is not registered."""
        return self._tools.get(name)

    def list_metadata(self) -> list[dict[str, Any]]:
        """Deep-copied metadata in registration order. Callers cannot mutate internals."""
        return [tool.to_metadata() for tool in self._tools.values()]

    def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        context: Any,
    ) -> Any:
        """Validate ``arguments``, apply defaults, call ``handler(context, **kwargs)``.

        :class:`~dbtools.errors.ToolError` raised by the handler is not caught.
        Unknown tools, unknown parameters, missing required values, and type
        mismatches fail closed as :class:`~dbtools.errors.BadParamError`.
        """
        if not isinstance(name, str) or not name.strip():
            raise BadParamError(
                "도구 이름이 비어 있습니다",
                hint="tools/list의 name을 그대로 전달하세요",
            )
        tool = self._tools.get(name)
        if tool is None:
            raise BadParamError(
                f"등록되지 않은 도구: {name}",
                hint="tools/list의 name을 그대로 전달하세요",
            )
        validated = _validate_arguments(tool, arguments)
        return tool.handler(context, **validated)


def _validate_arguments(
    tool: ToolDefinition,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if arguments is None:
        incoming: dict[str, Any] = {}
    elif isinstance(arguments, dict):
        incoming = dict(arguments)
    else:
        raise BadParamError(
            f"{tool.name}: arguments는 object여야 합니다",
            hint="JSON 객체로 파라미터를 전달하세요",
        )

    known = {spec.name: spec for spec in tool.params}
    unknown = [key for key in incoming if key not in known]
    if unknown:
        raise BadParamError(
            f"{tool.name}: 알 수 없는 파라미터 {unknown[0]!r}",
            hint="해당 도구의 params에 선언된 이름만 전달하세요",
        )

    validated: dict[str, Any] = {}
    for spec in tool.params:
        if spec.name in incoming:
            validated[spec.name] = _coerce_param(tool.name, spec, incoming[spec.name])
            continue
        if spec.required:
            raise BadParamError(
                f"{tool.name}: 필수 파라미터 {spec.name!r}가 없습니다",
                hint=f"{spec.name} ({spec.type}) 값을 전달하세요",
            )
        if spec.has_default():
            validated[spec.name] = copy.deepcopy(spec.default)
    return validated


def _coerce_param(tool_name: str, spec: ParamSpec, value: Any) -> Any:
    if value is None:
        raise BadParamError(
            f"{tool_name}: {spec.name}에 null은 허용되지 않습니다",
            hint=f"{spec.name}은(는) {spec.type} 이어야 합니다",
        )
    expected = spec.type
    if expected == "bool":
        if type(value) is not bool:
            raise BadParamError(
                f"{tool_name}: {spec.name}은(는) bool 이어야 합니다",
                hint=f"{spec.name}에 true 또는 false만 전달하세요",
            )
        return value
    if expected == "int":
        if type(value) is not int:
            raise BadParamError(
                f"{tool_name}: {spec.name}은(는) int 이어야 합니다",
                hint=f"{spec.name}에 정수만 전달하세요 (bool은 인정하지 않습니다)",
            )
        return value
    if expected == "number":
        if type(value) not in (int, float):
            raise BadParamError(
                f"{tool_name}: {spec.name}은(는) number 이어야 합니다",
                hint=f"{spec.name}에 정수 또는 실수를 전달하세요 (bool은 인정하지 않습니다)",
            )
        return value
    if expected == "string":
        if type(value) is not str:
            raise BadParamError(
                f"{tool_name}: {spec.name}은(는) string 이어야 합니다",
                hint=f"{spec.name}에 문자열을 전달하세요",
            )
        return value
    if expected == "list":
        if type(value) is not list:
            raise BadParamError(
                f"{tool_name}: {spec.name}은(는) list 이어야 합니다",
                hint=f"{spec.name}에 배열을 전달하세요",
            )
        return value
    if expected == "dict":
        if type(value) is not dict:
            raise BadParamError(
                f"{tool_name}: {spec.name}은(는) dict 이어야 합니다",
                hint=f"{spec.name}에 객체를 전달하세요",
            )
        return value
    raise BadParamError(
        f"{tool_name}: 지원하지 않는 파라미터 타입 {expected}",
        hint="string/int/number/bool/list/dict 만 허용됩니다",
    )


__all__ = ["ParamSpec", "ToolDefinition", "ToolRegistry"]
