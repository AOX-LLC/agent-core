"""Regenerate the outline blocks of docs/api.md from the code.

    uv run python tools/gen_api_docs.py           # rewrite the blocks
    uv run python tools/gen_api_docs.py --check   # exit 1 if docs/api.md is out of date

A block starts at `<!-- api:begin <module> [brief] [Name ...] -->` and ends at
`<!-- api:end <module> -->`. Everything between them is rewritten from the module's
`__all__` (or from the listed names): kind, signature and the first sentence of
the docstring. `brief` drops signatures and members and names the defining module
instead. Prose outside the blocks is never touched.

Standard library only, apart from the package it documents.
"""

import argparse
import enum
import importlib
import inspect
import re
import sys
import types
import typing
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

DOCUMENT = Path(__file__).resolve().parents[1] / "docs" / "api.md"
REGENERATE_COMMAND = "uv run python tools/gen_api_docs.py"

BLOCK = re.compile(
    r"(?P<begin><!-- api:begin (?P<module>\S+)(?P<options>[^>]*?)-->)\n"
    r".*?"
    r"(?P<end><!-- api:end (?P=module) -->)",
    re.DOTALL,
)
# Dotted module prefixes in a rendered value: datetime.timedelta -> timedelta.
QUALIFIER = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)+(?=[A-Za-z_])")
AUTO_DOCUMENTED = ("An enumeration.",)
OVERLOAD_DOC = "Helper for @overload"


def summary(documented: object) -> str:
    """The first sentence of a docstring, on one line; empty if there is none."""
    doc = getattr(documented, "__doc__", None)
    if not isinstance(doc, str) or doc in AUTO_DOCUMENTED or doc.startswith(OVERLOAD_DOC):
        return ""
    if isinstance(documented, type) and doc.startswith(f"{documented.__name__}("):
        return ""  # a dataclass-style generated docstring
    paragraph = inspect.cleandoc(doc).split("\n\n")[0]
    one_line = " ".join(paragraph.split())
    protected = one_line.replace("e.g.", "e<dot>g<dot>").replace("i.e.", "i<dot>e<dot>")
    sentence = re.split(r"(?<=[.!?])\s", protected, maxsplit=1)[0]
    return sentence.replace("<dot>", ".")


def annotation(tp: object) -> str:
    """An annotation as a reader would write it: no Annotated metadata, no module paths."""
    if isinstance(tp, str):
        return tp
    if tp is type(None):
        return "None"
    if tp is Ellipsis:
        return "..."
    if isinstance(tp, list):
        return "[" + ", ".join(annotation(item) for item in tp) + "]"
    origin = typing.get_origin(tp)
    arguments = typing.get_args(tp)
    if origin is typing.Annotated:
        return annotation(arguments[0])
    if origin is typing.Union or isinstance(tp, types.UnionType):
        return " | ".join(annotation(item) for item in arguments)
    if origin is typing.Literal:
        return "Literal[" + ", ".join(repr(item) for item in arguments) + "]"
    if origin is not None:
        name = getattr(origin, "__name__", None) or str(origin)
        return f"{name}[{', '.join(annotation(item) for item in arguments)}]" if arguments else name
    return str(getattr(tp, "__name__", tp))


def default(value: object) -> str:
    """A default as source would show it; `...` for anything that has no short literal form."""
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if value is None or isinstance(value, bool | int | float):
        return repr(value)
    if isinstance(value, str) and len(value) <= 40:
        return repr(value)
    if isinstance(value, timedelta):
        return QUALIFIER.sub("", repr(value))
    if isinstance(value, tuple | frozenset | dict) and not value:
        return {tuple: "()", frozenset: "frozenset()", dict: "{}"}[type(value)]
    return "..."


def signature_of(function: Callable[..., object], *, drop_first: bool = False) -> str:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return "(...)"
    parameters = list(signature.parameters.values())
    if drop_first and parameters and parameters[0].name in {"self", "cls"}:
        parameters = parameters[1:]
    parts: list[str] = []
    keyword_marker_due = True
    positional_only_seen = False
    for parameter in parameters:
        if parameter.kind is parameter.POSITIONAL_ONLY:
            positional_only_seen = True
        elif positional_only_seen:
            parts.append("/")
            positional_only_seen = False
        if parameter.kind is parameter.VAR_POSITIONAL:
            keyword_marker_due = False
        if parameter.kind is parameter.KEYWORD_ONLY and keyword_marker_due:
            parts.append("*")
            keyword_marker_due = False
        text = {parameter.VAR_POSITIONAL: "*", parameter.VAR_KEYWORD: "**"}.get(parameter.kind, "")
        text += parameter.name
        if parameter.annotation is not parameter.empty:
            text += f": {annotation(parameter.annotation)}"
        if parameter.default is not parameter.empty:
            text += f" = {default(parameter.default)}"
        parts.append(text)
    if positional_only_seen:
        parts.append("/")
    returns = ""
    if signature.return_annotation is not signature.empty:
        returns = f" -> {annotation(signature.return_annotation)}"
    return f"({', '.join(parts)}){returns}"


def kind_of(value: object) -> str:
    if isinstance(value, types.ModuleType):
        return "module"
    if inspect.isclass(value):
        if issubclass(value, enum.Enum):
            return "enum"
        if getattr(value, "_is_protocol", False):
            return "protocol"
        if issubclass(value, BaseException):
            return "exception"
        if hasattr(value, "model_fields"):
            return "model"
        return "class"
    if inspect.iscoroutinefunction(value):
        return "async function"
    if inspect.isfunction(value):
        return "function"
    if typing.get_origin(value) is not None or isinstance(value, types.UnionType):
        return "type alias"
    return "constant"


def constant_value(name: str, value: object) -> str:
    """Show a constant's value when it is a short, stable literal; else only its type."""
    if name.startswith("__") and name.endswith("__"):
        return f"`{type(value).__name__}`"
    if isinstance(value, bool | int | float | timedelta) or (
        isinstance(value, str) and len(value) <= 60
    ):
        return f"`{default(value) if not isinstance(value, str) else repr(value)}`"
    return f"`{type(value).__name__}`"


def method_line(name: str, raw: object) -> str | None:
    if isinstance(raw, property):
        return f"  - `{name}` (property){_dash(summary(raw.fget))}"
    function = raw.__func__ if isinstance(raw, classmethod | staticmethod) else raw
    if not inspect.isfunction(function):
        return None
    labels = []
    if isinstance(raw, classmethod):
        labels.append("classmethod")
    if isinstance(raw, staticmethod):
        labels.append("staticmethod")
    if inspect.iscoroutinefunction(function):
        labels.append("async")
    if function.__module__ == "typing":  # typing's _overload_dummy
        # A Protocol whose method is only overloads: the last stub is all that is left.
        rendered = "(...)"
        labels.append("overloaded")
    else:
        rendered = signature_of(function, drop_first=not isinstance(raw, staticmethod))
    label = f" ({', '.join(labels)})" if labels else ""
    return f"  - `{name}{rendered}`{label}{_dash(summary(function))}"


def member_lines(owner: type) -> list[str]:
    """What the class itself defines: enum values, model fields, public methods, properties."""
    lines: list[str] = []
    if issubclass(owner, enum.Enum):
        lines.extend(f"  - `{member.name} = {member.value!r}`" for member in owner)
    fields = getattr(owner, "model_fields", None)
    if fields:
        rendered = []
        for field_name, field in fields.items():
            text = f"{field_name}: {annotation(field.annotation)}"
            if not field.is_required():
                text += f" = {default(field.default) if field.default_factory is None else '...'}"
            rendered.append(text)
        lines.append(f"  - fields: `{', '.join(rendered)}`")
    for name, raw in vars(owner).items():
        if name.startswith(("_", "model_")):
            continue
        line = method_line(name, raw)
        if line is not None:
            lines.append(line)
    return lines


def _dash(text: str) -> str:
    return f" \u2014 {text}" if text else ""


def entry(name: str, value: object, *, brief: bool, module: str) -> list[str]:
    kind = kind_of(value)
    head = f"- **`{name}`** ({kind})"
    if kind == "constant":
        return [f"{head} {constant_value(name, value)}"]
    if kind == "type alias":
        return [f"{head} `{annotation(value)}`"]
    if brief:
        home = getattr(value, "__module__", None)
        where = f" in `{home}`" if home and home != module and inspect.isclass(value) else ""
        return [f"{head}{where}{_dash(summary(value))}"]
    if kind in {"function", "async function"}:
        return [f"{head} `{name}{signature_of(value)}`{_dash(summary(value))}"]  # type: ignore[arg-type]
    if kind == "module":
        return [f"{head}{_dash(summary(value))}"]
    if not inspect.isclass(value):
        return [head]
    constructor = ""
    if kind in {"class", "exception"}:
        constructor = f" `{name}{signature_of(value).split(' -> ')[0]}`"
    return [f"{head}{constructor}{_dash(summary(value))}", *member_lines(value)]


def block_body(module_name: str, options: list[str]) -> str:
    if module_name != "aox_agent_core" and not module_name.startswith("aox_agent_core."):
        raise SystemExit(
            f"refusing to import {module_name}: only aox_agent_core modules are documented"
        )
    module = importlib.import_module(module_name)
    brief = "brief" in options
    names = [option for option in options if option != "brief"] or list(module.__all__)
    lines = [
        f"<!-- generated by tools/gen_api_docs.py from `{module_name}`; "
        "do not edit between the markers -->",
        "",
    ]
    for name in names:
        lines.extend(entry(name, getattr(module, name), brief=brief, module=module_name))
    return "\n".join(lines)


def render(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        options = match["options"].split()
        body = block_body(match["module"], options)
        return f"{match['begin']}\n{body}\n{match['end']}"

    return BLOCK.sub(replace, text)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate the outline blocks of docs/api.md.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    parser.add_argument("--path", type=Path, default=DOCUMENT, help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    current = arguments.path.read_text(encoding="utf-8")
    if not BLOCK.search(current):
        print(f"{arguments.path} has no api:begin/api:end blocks.", file=sys.stderr)
        return 2
    updated = render(current)
    if arguments.check:
        if updated != current:
            print(f"{arguments.path} is out of date. Run: {REGENERATE_COMMAND}", file=sys.stderr)
            return 1
        return 0
    if updated != current:
        arguments.path.write_text(updated, encoding="utf-8")
        print(f"Updated {arguments.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
