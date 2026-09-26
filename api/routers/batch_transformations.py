"""
Batch transformation router. LOCAL addition to Open Notebook, not part of upstream.

Upstream can only run a transformation on one source at a time
(``POST /api/sources/{source_id}/insights``). A notebook holding ~100 papers
therefore needs ~100 clicks per transformation. This router adds the notebook-wide
equivalent, and skips the sources that already carry that transformation's insight
so it can be re-run safely after a partial or failed pass.

Endpoints (all under ``/api``):

- ``GET  /notebooks/{notebook_id}/batch-transformations``
      every transformation with, for this notebook, how many of its sources already
      have that insight and how many are missing. Populates the picker and doubles
      as the progress poll: re-fetch and watch ``missing`` fall.
- ``POST /notebooks/{notebook_id}/batch-transformations/{transformation_id}/apply``
      queue one ``run_transformation`` job per source that is missing the insight.
      Returns immediately; the worker does the work.

How "already has this insight" is decided
-----------------------------------------
``open_notebook/graphs/transformation.py`` writes the insight with
``source.add_insight(transformation.title, ...)``, so ``source_insight.insight_type``
holds the transformation's **title** verbatim. That string equality is the whole
test. Two consequences worth knowing:

- Renaming a transformation makes its past insights invisible to this check, so the
  next apply re-runs every source under the new title.
- Two transformations sharing a title are indistinguishable here.

Both match how the rest of the app already behaves; nothing is recorded that would
let us do better without a schema change.

Nothing is loaded into GPU memory here. Each queued job calls the chat model when
the worker reaches it, exactly as the single-source endpoint does; if no model is
loaded the jobs fail with the model server's "model is not loaded" error.

Database access and job submission go through the two module-level helpers
``_query`` and ``_submit``, which import their dependencies lazily, so this module
imports cleanly outside the container and the tests can substitute them:
see ``local_tests/test_batch_transformations.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(tags=["batch-transformations"])


class TransformationCounts(BaseModel):
    id: str
    name: Optional[str] = None
    title: str = Field(description="Also the insight_type written on the insight")
    applied: int = Field(description="Sources in this notebook that already have it")
    missing: int = Field(description="Sources in this notebook that do not")


class BatchStatusResponse(BaseModel):
    notebook_id: str
    total_sources: int
    transformations: List[TransformationCounts] = Field(default_factory=list)


class BatchApplyResponse(BaseModel):
    notebook_id: str
    transformation_id: str
    title: str
    total_sources: int
    skipped: int = Field(description="Already had the insight; no job queued")
    queued: int = Field(description="Jobs submitted")
    failed: int = Field(description="Sources whose job could not be submitted")


async def _query(sql: str, variables: Optional[Dict[str, Any]] = None) -> Any:
    """Run SurrealQL. Imported lazily so this module loads outside the container."""
    from open_notebook.database.repository import repo_query

    return await repo_query(sql, variables or {})


def _submit(source_id: str, transformation_id: str) -> str:
    """Queue one upstream run_transformation job. Lazy import, as above."""
    from surreal_commands import submit_command

    return submit_command(
        "open_notebook",
        "run_transformation",
        {"source_id": source_id, "transformation_id": transformation_id},
    )


def _record_id(value: str) -> Any:
    from open_notebook.database.repository import ensure_record_id

    return ensure_record_id(value)


async def _source_ids_in_notebook(notebook_id: str) -> List[str]:
    """
    The notebook's sources, as id strings.

    ``reference`` is the source -> notebook edge (``in`` is the source, ``out`` the
    notebook), the same traversal the upstream source list uses. The edge table is
    filtered to source records defensively: a stray non-source edge would otherwise
    be counted as a source that can never gain an insight, and ``missing`` would
    never reach zero.
    """
    rows = await _query(
        "SELECT VALUE in FROM reference WHERE out = $notebook_id",
        {"notebook_id": _record_id(notebook_id)},
    )
    ids = [str(row) for row in (rows or [])]
    return [source_id for source_id in ids if source_id.startswith("source:")]


async def _insight_types_by_source(source_ids: List[str]) -> Dict[str, set]:
    """{source id -> {insight_type, ...}} for the given sources."""
    if not source_ids:
        return {}

    rows = await _query(
        "SELECT source, insight_type FROM source_insight WHERE source IN $source_ids",
        {"source_ids": [_record_id(source_id) for source_id in source_ids]},
    )

    by_source: Dict[str, set] = {}
    for row in rows or []:
        source_id = str(row.get("source", ""))
        insight_type = row.get("insight_type")
        if not source_id or insight_type is None:
            continue
        by_source.setdefault(source_id, set()).add(str(insight_type))
    return by_source


async def _transformations() -> List[Dict[str, Any]]:
    rows = await _query("SELECT id, name, title FROM transformation")
    return [dict(row) for row in (rows or [])]


def _missing_sources(
    source_ids: List[str], by_source: Dict[str, set], title: str
) -> List[str]:
    """Sources with no insight whose insight_type equals this transformation's title."""
    return [
        source_id
        for source_id in source_ids
        if title not in by_source.get(source_id, set())
    ]


@router.get(
    "/notebooks/{notebook_id}/batch-transformations",
    response_model=BatchStatusResponse,
)
async def get_batch_status(notebook_id: str) -> BatchStatusResponse:
    """Per-transformation applied/missing counts for one notebook."""
    source_ids = await _source_ids_in_notebook(notebook_id)
    by_source = await _insight_types_by_source(source_ids)
    transformations = await _transformations()

    counts: List[TransformationCounts] = []
    for transformation in transformations:
        title = str(transformation.get("title") or "")
        if not title:
            # Without a title there is no insight_type to compare against, so this
            # transformation can be neither counted nor deduplicated. Skip it
            # rather than report a number that would always read as "all missing".
            logger.warning(
                f"Transformation {transformation.get('id')} has no title; "
                "excluded from batch transformation counts"
            )
            continue
        missing = len(_missing_sources(source_ids, by_source, title))
        counts.append(
            TransformationCounts(
                id=str(transformation.get("id", "")),
                name=transformation.get("name"),
                title=title,
                applied=len(source_ids) - missing,
                missing=missing,
            )
        )

    counts.sort(key=lambda item: item.title.lower())
    return BatchStatusResponse(
        notebook_id=notebook_id,
        total_sources=len(source_ids),
        transformations=counts,
    )


@router.post(
    "/notebooks/{notebook_id}/batch-transformations/{transformation_id}/apply",
    response_model=BatchApplyResponse,
    status_code=202,
)
async def apply_batch_transformation(
    notebook_id: str, transformation_id: str
) -> BatchApplyResponse:
    """
    Queue the transformation for every source in the notebook that lacks its insight.

    Returns 202 as soon as the jobs are submitted. Re-running is safe: sources that
    gained the insight in the meantime are skipped on the next call.
    """
    transformations = await _transformations()
    transformation = next(
        (
            item
            for item in transformations
            if str(item.get("id", "")) == transformation_id
        ),
        None,
    )
    if transformation is None:
        raise HTTPException(status_code=404, detail="Transformation not found")

    title = str(transformation.get("title") or "")
    if not title:
        raise HTTPException(
            status_code=422,
            detail=(
                "Transformation has no title, so its insights cannot be identified. "
                "Give it a title before applying it in batch."
            ),
        )

    source_ids = await _source_ids_in_notebook(notebook_id)
    if not source_ids:
        raise HTTPException(
            status_code=404, detail="Notebook has no sources (or does not exist)"
        )

    by_source = await _insight_types_by_source(source_ids)
    missing = _missing_sources(source_ids, by_source, title)

    logger.info(
        f"Batch transformation '{title}' on {notebook_id}: "
        f"{len(missing)} of {len(source_ids)} sources missing the insight"
    )

    queued = 0
    failed = 0
    for source_id in missing:
        try:
            _submit(source_id, transformation_id)
            queued += 1
        except Exception as exc:
            # One bad submission must not abandon the rest of the batch; the source
            # keeps its missing status and is picked up by the next apply.
            failed += 1
            logger.error(
                f"Failed to queue run_transformation for {source_id} "
                f"({transformation_id}): {exc}"
            )

    logger.info(
        f"Batch transformation '{title}' on {notebook_id}: "
        f"queued {queued}, skipped {len(source_ids) - len(missing)}, failed {failed}"
    )

    return BatchApplyResponse(
        notebook_id=notebook_id,
        transformation_id=transformation_id,
        title=title,
        total_sources=len(source_ids),
        skipped=len(source_ids) - len(missing),
        queued=queued,
        failed=failed,
    )
