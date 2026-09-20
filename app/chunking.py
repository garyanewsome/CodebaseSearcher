"""Walk a repo and split its source files into chunks to embed.

Line-based with blank-line-aware boundaries, language-agnostic — not
AST/tree-sitter parsing. Same pragmatic bar as Athenaeum's own markdown
chunker (its README calls it "functional, not tuned; revisit if a
consumer's retrieval quality suffers") rather than the docstring
promising more than it delivers. Good enough to find the right file and
the right neighborhood of it; not meant to return exact function
boundaries every time.
"""

import os
from pathlib import Path
from typing import Iterator

CHUNK_MAX_LINES = 120
CHUNK_MAX_CHARS = 6000

SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".sh", ".sql",
    ".md", ".yaml", ".yml", ".json", ".toml",
}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", "target", "vendor", ".pytest_cache", "coverage", "static",
}

MAX_FILE_BYTES = 400_000  # skip generated/vendored giants, not real source


def _iter_source_files(repo_path: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _chunk_lines(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    buf: list[str] = []
    buf_chars = 0

    def flush():
        if buf and "".join(buf).strip():
            chunks.append("\n".join(buf))

    for line in lines:
        buf.append(line)
        buf_chars += len(line) + 1
        over_size = len(buf) >= CHUNK_MAX_LINES or buf_chars >= CHUNK_MAX_CHARS
        if over_size and line.strip() == "":
            flush()
            buf, buf_chars = [], 0
    flush()
    # A file with no convenient blank-line break near the threshold (e.g.
    # densely packed minified-ish code) still needs to end somewhere —
    # anything left in buf past the loop is exactly that trailing chunk.
    return chunks


def chunk_repo(repo_path: Path) -> list[dict]:
    """Returns [{file_path (relative, str), content}, ...]."""
    chunks: list[dict] = []
    for path in _iter_source_files(repo_path):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.strip():
            continue
        rel = str(path.relative_to(repo_path))
        for content in _chunk_lines(text.split("\n")):
            chunks.append({"file_path": rel, "content": content})
    return chunks
