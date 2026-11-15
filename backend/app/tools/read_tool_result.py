"""Schema only; dispatch is bound to a server-owned request-local store."""
from langchain_core.tools import tool


@tool
async def read_tool_result(result_handle: str, source_ids: list[str | int] | None = None,
                           offset: int = 0, limit: int = 3) -> str:
    """Read original evidence from a saved result in this request.

    Args:
        result_handle: Exact rN handle returned by a tool result index, never a path.
        source_ids: Optional exact sN source aliases from that saved result.
        offset: Zero-based source offset for paging. Never a character offset.
        limit: Source count per page, 1 to 20. Whole source bodies only.
    """
    raise ValueError('read_tool_result requires server-side request-local store dispatch')
