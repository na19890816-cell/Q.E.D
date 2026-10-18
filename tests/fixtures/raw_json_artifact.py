"""Synthetic software fixtures, not ProStock2 evidence."""
import hashlib

from analytics.python.data_contracts.raw_json_artifact import CONTRACT, _PROVENANCE


def artifact(raw=b'{\n  "synthetic": [{"symbol": "TEST", "value": null}]\n}\n', *, identity="sample", role="original", parents=None):
    return raw, {
        "contract_id": CONTRACT, "artifact_schema_version": 1, "source_system": "prostock2",
        "export_id": "export-" + identity, "artifact_id": identity, "artifact_kind": "snapshot",
        "serialization": "single_json_document", "media_type": "application/json",
        "exported_at": "2026-10-18T00:00:00Z", "complete": True,
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
        "artifact_role": role, "derived_from": parents or [],
        "provenance": {name: None for name in sorted(_PROVENANCE)},
        "provenance_gaps": sorted(_PROVENANCE),
    }
