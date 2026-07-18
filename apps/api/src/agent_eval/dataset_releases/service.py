from agent_eval.db.models import DatasetRelease
from agent_eval.evaluation_cases.service import canonical_sha256


def release_manifest_payload(release: DatasetRelease) -> dict[str, object]:
    return {
        "cases": [
            {
                "evaluation_case_id": str(link.evaluation_case_id),
                "position": link.position,
                "record_hash": link.record_hash,
            }
            for link in sorted(release.case_links, key=lambda item: item.position)
        ],
        "description": release.description,
        "name": release.name,
        "release_id": str(release.id),
        "schema_version": "curated-dataset-release-v1",
    }


def release_manifest_hash(release: DatasetRelease) -> str:
    return canonical_sha256(release_manifest_payload(release))
