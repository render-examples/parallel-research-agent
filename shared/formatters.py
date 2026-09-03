"""Format Parallel API responses for Claude's context window.

These helpers convert raw Parallel Search and Extract results into compact
text that fits efficiently inside a tool_result block. The goal is maximum
signal per token — Claude needs the content, not the JSON scaffolding.
"""

from __future__ import annotations

MAX_EXCERPT_CHARS = 1500
MAX_EXTRACT_CHARS = 3000


def format_search_results(search_response) -> str:
    """Turn a Parallel Search response into a numbered source list.

    Each result becomes:
        [1] Title
        URL: https://...
        Published: 2026-01-15 (if available)
        Excerpt: ...

    This format gives Claude structured references it can cite in the
    final report while keeping token count reasonable.
    """
    lines: list[str] = []

    for i, result in enumerate(search_response.results, 1):
        lines.append(f"[{i}] {result.title}")
        lines.append(f"    URL: {result.url}")

        if getattr(result, "publish_date", None):
            lines.append(f"    Published: {result.publish_date}")

        for excerpt in result.excerpts:
            truncated = excerpt[:MAX_EXCERPT_CHARS]
            if len(excerpt) > MAX_EXCERPT_CHARS:
                truncated += " [...]"
            lines.append(f"    Excerpt: {truncated}")

        lines.append("")

    if not lines:
        return "No results found."

    return "\n".join(lines)


def format_extract_results(extract_response) -> str:
    """Turn a Parallel Extract response into labeled page contents.

    Each extracted page becomes:
        === Page: https://... ===
        <content>

    Content is truncated to keep the context window manageable when the
    agent extracts multiple pages in one call.
    """
    sections: list[str] = []

    results = getattr(extract_response, "results", [])
    if not results:
        results = extract_response if isinstance(extract_response, list) else []

    for result in results:
        url = getattr(result, "url", "unknown")
        content = getattr(result, "content", "") or getattr(result, "text", "") or ""

        truncated = content[:MAX_EXTRACT_CHARS]
        if len(content) > MAX_EXTRACT_CHARS:
            truncated += "\n\n[... content truncated]"

        sections.append(f"=== Page: {url} ===\n{truncated}")

    if not sections:
        return "No content could be extracted."

    return "\n\n".join(sections)
