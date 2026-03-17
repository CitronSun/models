# tool_registry.py
from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Any

from langchain_core.tools import StructuredTool, BaseTool


def register_activities_as_tools(
    activities: Iterable[Callable[..., Any]],
    *,
    name_prefix: str = "",
    include: Optional[set[str]] = None,
    exclude: Optional[set[str]] = None,
) -> List[BaseTool]:
    """
    Convert Temporal activity functions into LangChain tools.

    - Tool name defaults to function.__name__ (optionally with prefix).
    - Tool description is taken from function docstring (triple-quoted comment).
    - Tool input schema is inferred from function signature + type hints.
    """
    tools: List[BaseTool] = []

    for fn in activities:
        fn_name = getattr(fn, "__name__", "anonymous")

        if include and fn_name not in include:
            continue
        if exclude and fn_name in exclude:
            continue

        tool_name = f"{name_prefix}{fn_name}"

        # IMPORTANT:
        # StructuredTool.from_function will:
        # - use docstring as description
        # - derive args schema from type hints
        # - call the function when invoked
        #
        # If your activity functions are async, set coroutine=fn instead.
        if callable(fn):
            tool = StructuredTool.from_function(
                func=fn,                 # if async: use coroutine=fn and func=None
                name=tool_name,
                description=(fn.__doc__ or "").strip() or f"Temporal activity: {fn_name}",
            )
            tools.append(tool)

    return tools