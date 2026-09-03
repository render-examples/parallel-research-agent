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
        === Page: Title — https://... ===
        <markdown excerpts>

    Extract returns `excerpts` (a list of markdown strings aligned to the
    objective) and, only when explicitly requested, `full_content`. Prefer
    the excerpts: they're already scoped to what the agent asked for.
    """
    sections: list[str] = []

    for result in getattr(extract_response, "results", []) or []:
        url = getattr(result, "url", "unknown")
        title = getattr(result, "title", None)
        header = f"{title} — {url}" if title else url

        excerpts = getattr(result, "excerpts", None) or []
        content = "\n\n".join(excerpts)
        if not content:
            content = getattr(result, "full_content", "") or ""

        if not content:
            sections.append(f"=== Page: {header} ===\n[no content returned]")
            continue

        truncated = content[:MAX_EXTRACT_CHARS]
        if len(content) > MAX_EXTRACT_CHARS:
            truncated += "\n\n[... content truncated]"

        sections.append(f"=== Page: {header} ===\n{truncated}")

    # Per-URL failures come back in `errors` rather than raising, so surface
    # them — otherwise the agent silently treats a dead URL as a dead end.
    for error in getattr(extract_response, "errors", None) or []:
        url = getattr(error, "url", "unknown")
        message = getattr(error, "message", None) or getattr(error, "detail", "failed")
        sections.append(f"=== Page: {url} ===\n[extraction failed: {message}]")

    if not sections:
        return "No content could be extracted."

    return "\n\n".join(sections)
