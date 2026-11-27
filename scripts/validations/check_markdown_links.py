#!/usr/bin/env python3
"""Check tracked root and docs Markdown files for broken local links."""

import re
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN = MarkdownIt("commonmark", {"html": True})
HTML_ID = re.compile(r"\b(?:id|name)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def slug(text):
    """Approximate GitHub heading IDs, preserving Unicode letters and underscores."""
    text = text.lower()
    text = "".join(c for c in text if c in "-_ " or unicodedata.category(c)[0] not in "PC")
    return text.replace(" ", "-")


def document(path):
    """Return parsed links and heading/HTML anchors for a Markdown file."""
    tokens = MARKDOWN.parse(path.read_text(encoding="utf-8"))
    anchors, links, counts = set(), [], Counter()
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[i + 1]
            text = "".join(
                child.content for child in inline.children if child.type in {"text", "code_inline", "image"}
            )
            base = slug(text)
            number = counts[base]
            candidate = f"{base}-{number}" if number else base
            while candidate in anchors:
                number += 1
                candidate = f"{base}-{number}"
            counts[base] += 1
            anchors.add(candidate)
        if token.type in {"html_block", "html_inline"}:
            anchors.update(HTML_ID.findall(token.content))
        for child in token.children or []:
            if child.type in {"link_open", "image"}:
                links.append((child.attrGet("href") if child.type == "link_open" else child.attrGet("src"), token.map))
            if child.type == "html_inline":
                anchors.update(HTML_ID.findall(child.content))
    return links, anchors


def check(paths):
    """Return diagnostics for local file and Markdown fragment references."""
    cache = {}

    def parse(path):
        if path not in cache:
            cache[path] = document(path)
        return cache[path]

    errors = []
    for source in paths:
        if not source.is_file():
            continue
        links, _ = parse(source)
        for link, location in links:
            if not link:
                continue
            url = urlsplit(link)
            # Absolute paths are application URLs, not repository-relative files.
            if url.scheme or url.netloc or url.path.startswith("/"):
                continue
            target = (source.parent / unquote(url.path)).resolve() if url.path else source
            line = location[0] + 1 if location else 1
            try:
                display = source.relative_to(ROOT)
            except ValueError:
                display = source
            prefix = f"{display}:{line}: {link}"
            if not target.exists():
                errors.append(f"{prefix}: target does not exist")
            elif url.fragment and target.is_file() and target.suffix.lower() == ".md":
                if unquote(url.fragment) not in parse(target)[1]:
                    errors.append(f"{prefix}: anchor does not exist")
    return errors


def main():
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md", "docs/**/*.md"], cwd=ROOT, check=True, capture_output=True
    )
    paths = []
    for name in result.stdout.decode().split("\0"):
        path = Path(name)
        if name and (path.parent == Path(".") or path.parts[0] == "docs") and path.suffix == ".md":
            paths.append(ROOT / path)
    errors = check(paths)
    print("\n".join(errors) if errors else f"Checked {len(paths)} Markdown files; local links are valid.")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
