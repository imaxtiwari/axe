#!/usr/bin/env python3
"""Static policy check: model selects must be scoped.

This script checks repository and service files for raw ``select(Model)`` calls
that are neither routed through ``IsolationService`` helpers
(``.select_for``, ``.scope``, ``.scope_for_context``) nor followed by an
explicit ``pm_id`` / ``fund_entity_id`` filter on the same statement, and that
do not target a model explicitly marked ``isolation_scope = "global"``.

Run it in CI:

    python scripts/check_isolation_policy.py

Exit code 0 means no unscoped selects were found in the inspected files;
exit code 1 means a potential isolation violation needs review.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "axe"
# Inspect data access layers only; agents may contain ad-hoc read patterns.
INSPECT_DIRS = [SRC / "db", SRC / "services"]

SELECT_RE = re.compile(r"(?<![\w.])select\s*\(")
ISOLATION_SERVICE_RE = re.compile(
    r"IsolationService\.(select_for|scope|scope_for_context)\s*\(",
    re.MULTILINE | re.DOTALL,
)
EXPLICIT_FILTER_RE = re.compile(
    r"\.(where|filter)\s*\([^)]*(pm_id|fund_entity_id)\s*[=!]=",
    re.MULTILINE | re.DOTALL,
)
EXPLICIT_PK_FILTER_RE = re.compile(
    # e.g. PMUser.id == self.pm_id or PMUser.id == pm_id
    r"[A-Z][A-Za-z0-9_]+\.id\s*==\s*(self\.)?\w+",
    re.MULTILINE | re.DOTALL,
)
NOQA_ISOLATION_RE = re.compile(
    r"#\s*(noqa:.*isolation|isolation:\s*(global|allowed|system-wide))",
    re.IGNORECASE,
)
GLOBAL_SCOPE_RE = re.compile(
    r"isolation_scope\s*=\s*['\"]global['\"]",
    re.MULTILINE,
)


def _looks_like_docstring_or_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("#") or ("\"" in stripped and "\"\"\"" in stripped)


def _global_models() -> set[str]:
    """Return the set of model names explicitly marked as global."""
    models: set[str] = set()
    models_file = SRC / "db" / "models.py"
    if not models_file.exists():
        return models
    source = models_file.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return models
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_start = node.lineno - 1
        class_end = node.end_lineno or class_start + 1
        class_source = "\n".join(source.splitlines()[class_start:class_end])
        if GLOBAL_SCOPE_RE.search(class_source):
            models.add(node.name)
    return models


def _statement_window(source: str, match_start: int) -> str:
    """Return a few lines around the select call for multiline inspection."""
    lines = source.splitlines()
    line_index = source[:match_start].count("\n")
    window = "\n".join(lines[max(0, line_index - 2) : line_index + 4])
    return window


def _is_safe(source: str, match_start: int, global_models: set[str]) -> bool:
    """Return True if the select at ``match_start`` is already scoped."""
    statement = _statement_window(source, match_start)

    if NOQA_ISOLATION_RE.search(statement):
        return True
    if ISOLATION_SERVICE_RE.search(statement):
        return True
    if EXPLICIT_FILTER_RE.search(statement):
        return True
    if EXPLICIT_PK_FILTER_RE.search(statement):
        return True

    # Check whether the select targets a global model: select(GlobalModel).
    global_match = re.search(r"select\s*\(\s*([A-Z][A-Za-z0-9_]+)", statement)
    if global_match and global_match.group(1) in global_models:
        return True

    return False


def check() -> list[str]:
    """Return a list of violation messages."""
    violations: list[str] = []
    global_models = _global_models()
    for inspect_dir in INSPECT_DIRS:
        for path in sorted(inspect_dir.rglob("*.py")):
            if path.name.startswith("test_") or path.name == "__init__.py":
                continue
            source = path.read_text(encoding="utf-8")
            for match in SELECT_RE.finditer(source):
                line_start = source.rfind("\n", 0, match.start()) + 1
                line_end = source.find("\n", match.start())
                line = source[line_start:line_end if line_end != -1 else None]
                if _looks_like_docstring_or_comment(line):
                    continue
                if _is_safe(source, match.start(), global_models):
                    continue
                stripped = line.strip()
                violations.append(f"{path.relative_to(ROOT)}: {stripped}")
    return violations


def main() -> int:
    violations = check()
    if violations:
        print("Isolation policy violations found (unscoped select(...) usage):")
        for v in violations:
            print(f"  {v}")
        print(
            "\nFix by routing the query through IsolationService.select_for(model), "
            "IsolationService.scope(stmt, model, pm_id), adding an explicit pm_id/"
            "fund_entity_id filter, or marking the model as isolation_scope='global' "
            "if it is intentionally unscoped."
        )
        return 1
    print("No isolation policy violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
