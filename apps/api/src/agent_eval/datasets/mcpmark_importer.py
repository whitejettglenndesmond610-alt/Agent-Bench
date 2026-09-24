"""MCPMark filesystem dataset importer.

Builds QuestionRecord entries from a local MCPMark checkout
(github.com/eval-sys/mcpmark, Apache-2.0). Each task lives at
``tasks/filesystem/<suite>/<category>/<task_id>/`` with three files:
``meta.json`` (metadata incl. ``meta_data.stateUrl``), ``description.md``
(the natural-language instruction) and ``verify.py`` (the programmatic state
checker executed by the worker at episode end).

The initial state zip is NOT downloaded at import time; the worker downloads
and caches it on demand. The MCPMark git commit is pinned into
``source_version`` so the importer's content hash changes whenever the pinned
checkout moves.
"""

import json
import subprocess
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from agent_eval.datasets.importer import (
    MCP_V1_COHORT_KEY,
    ImportedVersion,
    import_question_records,
)
from agent_eval.datasets.schema import QuestionRecord
from agent_eval.db.enums import Ability, Language, QuestionType

MCPMARK_FILESYSTEM_BENCHMARK = "MCPMark-Filesystem"
MCPMARK_SUITE_BENCHMARKS = {
    "easy": MCPMARK_FILESYSTEM_BENCHMARK,
    "standard": f"{MCPMARK_FILESYSTEM_BENCHMARK}-Standard",
}
MCPMARK_SOURCE = "mcpmark"
SUPPORTED_SUITES = ("easy", "standard")
DEFAULT_IMPORT_LIMIT: int | None = None
# Fallback file for task snapshots that are not a git checkout.
COMMIT_PIN_FILE = ".mcpmark-commit"


def mcpmark_task_ref(suite: str, category: str, task_id: str) -> str:
    return f"mcp:filesystem:{suite}/{category}/{task_id}"


def mcpmark_external_id(suite: str, category: str, task_id: str) -> str:
    return f"{suite}/{category}/{task_id}"


def mcpmark_commit_pin(repo_dir: Path) -> str:
    """Resolve the pinned MCPMark commit for a checkout or task snapshot."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        commit = result.stdout.strip()
        if result.returncode == 0 and len(commit) == 40:
            return commit
    except (OSError, subprocess.TimeoutExpired):
        pass
    pin_file = repo_dir / COMMIT_PIN_FILE
    if pin_file.is_file():
        commit = pin_file.read_text(encoding="utf-8").strip()
        if commit:
            return commit
    raise ValueError(
        f"cannot pin an MCPMark commit for {repo_dir}: not a git checkout and "
        f"no {COMMIT_PIN_FILE} file"
    )


def build_mcpmark_filesystem_records(
    repo_dir: Path, suite: str = "easy", *, limit: int | None = None
) -> list[QuestionRecord]:
    """Load MCPMark filesystem tasks for one suite from a local checkout.

    ``repo_dir`` is the repository root (or a snapshot containing
    ``tasks/filesystem/...``). Tasks are taken in ``category/task_id`` order;
    ``limit`` caps how many tasks are imported.
    """
    if suite not in SUPPORTED_SUITES:
        raise ValueError(
            f"unsupported MCPMark suite {suite!r}; expected one of {SUPPORTED_SUITES}"
        )
    tasks_root = repo_dir / "tasks" / "filesystem" / suite
    if not tasks_root.is_dir():
        raise ValueError(f"no MCPMark filesystem/{suite} tasks under {repo_dir}")
    commit = mcpmark_commit_pin(repo_dir)
    task_dirs = sorted(
        (path for path in tasks_root.glob("*/*") if path.is_dir()),
        key=lambda path: (path.parent.name, path.name),
    )
    records: list[QuestionRecord] = []
    for task_dir in task_dirs:
        meta_path = task_dir / "meta.json"
        description_path = task_dir / "description.md"
        verify_path = task_dir / "verify.py"
        if not (meta_path.is_file() and description_path.is_file() and verify_path.is_file()):
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or not isinstance(meta.get("meta_data"), dict):
            raise ValueError(f"{meta_path}: meta.json must contain a meta_data object")
        category = task_dir.parent.name
        task_id = task_dir.name
        records.append(
            QuestionRecord(
                benchmark=MCPMARK_SUITE_BENCHMARKS[suite],
                source=MCPMARK_SOURCE,
                source_version=f"mcpmark-{commit}",
                license_note="MCPMark (eval-sys), Apache-2.0 license",
                sampling_seed=0,
                external_id=mcpmark_external_id(suite, category, task_id),
                language=Language.EN,
                ability=Ability.AGENTIC,
                type=QuestionType.INTERACTIVE,
                prompt=description_path.read_text(encoding="utf-8"),
                reference_answer=mcpmark_task_ref(suite, category, task_id),
                requires_citations=False,
                domain=f"filesystem/{category}",
            )
        )
        if limit is not None and len(records) >= limit:
            break
    if not records:
        raise ValueError(f"no MCPMark filesystem/{suite} tasks found under {repo_dir}")
    return records


async def import_mcpmark_filesystem(
    session: AsyncSession,
    *,
    repo_dir: Path,
    suite: str = "easy",
    limit: int | None = DEFAULT_IMPORT_LIMIT,
) -> list[ImportedVersion]:
    records = build_mcpmark_filesystem_records(repo_dir, suite, limit=limit)
    return await import_question_records(
        session,
        records,
        cohort_key=MCP_V1_COHORT_KEY,
        held_out=False,
    )
