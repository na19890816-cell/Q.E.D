"""Pure contract tests; no assertion of real-data compatibility."""
from copy import deepcopy

import jsonschema
from pathlib import Path
import json
import pytest

from analytics.python.data_contracts.raw_json_artifact import (
    ArtifactLimits, validate_artifact_manifest, validate_document,
)
from tests.fixtures.raw_json_artifact import artifact


@pytest.mark.parametrize("raw", [b'{}', b' [] \r\n', b'{\n"a": [1, null, true, "Japanese \\u65e5"]\n}',
                                 b'{"quoted":"[\\\"{]"}', b'{"x":1.25}'])
def test_valid_documents(raw):
    assert isinstance(validate_document(raw), (dict, list))


@pytest.mark.parametrize("raw", [b'', b'null', b'1', b'"text"', b'{', b'{}{}', b'{}\n{}\n',
    b'{"x":1,"x":2}', b'{"x": {"y":1,"y":2}}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":-Infinity}', b'{"x":1e999}', b'{"x":"\xff"}', b'\xef\xbb\xbf{}',
    b'{"x":"\\ud800"}', b'{"\\udfff": 1}'])
def test_invalid_documents(raw):
    with pytest.raises(ValueError, match="^INVALID_SINGLE_JSON_DOCUMENT$"):
        validate_document(raw)


def test_depth_size_and_node_limits():
    for raw, limits in [(b'[[[]]]', ArtifactLimits(json_depth=2)),
                        (b'{}', ArtifactLimits(file_bytes=1)),
                        (b'[1,2,3]', ArtifactLimits(json_nodes=3))]:
        with pytest.raises(ValueError):
            validate_document(raw, limits)
    assert validate_document(b'[[[]]]', ArtifactLimits(json_depth=3)) == [[[]]]


def test_manifest_copy_and_json_schema():
    _, manifest = artifact()
    checked = validate_artifact_manifest(manifest)
    assert checked == manifest and checked is not manifest
    schema = json.loads((Path(__file__).resolve().parents[2] /
                         'qedschema/contracts/raw_json_artifact_v1.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(checked)
    checked['provenance_gaps'].clear()
    assert manifest['provenance_gaps']


@pytest.mark.parametrize("field,value", [
    ('artifact_schema_version', True), ('artifact_schema_version', 2), ('size_bytes', True),
    ('size_bytes', 0), ('complete', False), ('complete', 1), ('source_system', 'unknown'),
    ('raw_sha256', 'a'*63), ('serialization', 'jsonl'), ('artifact_id', '../escape'),
    ('artifact_kind', 'candidate_record'), ('exported_at', '2026-10-18T00:00:00'),
    ('exported_at', '2026-10-18T00:00:00+00:00'), ('exported_at', '2026-02-30T00:00:00Z'),
    ('artifact_role', 'derived'), ('provenance_gaps', []),
])
def test_bad_manifest(field, value):
    _, manifest = artifact()
    manifest[field] = value
    with pytest.raises(ValueError, match="^INVALID_ARTIFACT_MANIFEST$"):
        validate_artifact_manifest(manifest)


@pytest.mark.parametrize("key,value", [('available_at', '2026-10-17T00:00:00Z'),
    ('historical_captured_at', '2026-10-19T00:00:00Z'), ('market_date','2026-02-30'),
    ('logical_record_count', True), ('logical_record_count', -1), ('code_commit', 'a'*39),
    ('policy_hash', 'invalid'), ('source_revision', 1)])
def test_invalid_provenance(key, value):
    _, manifest = artifact()
    manifest['provenance'][key] = value
    with pytest.raises(ValueError):
        validate_artifact_manifest(manifest)


def test_extra_fields_and_incomplete_manifest_rejected():
    _, manifest = artifact()
    for change in ('extra', 'missing'):
        bad = deepcopy(manifest)
        if change == 'extra':
            bad['research_eligible'] = True
        else:
            del bad['raw_sha256']
        with pytest.raises(ValueError):
            validate_artifact_manifest(bad)


def test_derivation_requires_complete_distinct_parents():
    _, manifest = artifact(role='derived', parents=[{'raw_sha256':'b'*64,
        'transformation_code_sha256':'c'*64, 'spec_sha256':'d'*64, 'settings_sha256':'e'*64}])
    assert validate_artifact_manifest(manifest) == manifest
    for bad in [manifest['derived_from']*2, [{'raw_sha256':'b'*64}],
                [{**manifest['derived_from'][0], 'raw_sha256':manifest['raw_sha256']}]]:
        invalid = deepcopy(manifest)
        invalid['derived_from'] = bad
        with pytest.raises(ValueError):
            validate_artifact_manifest(invalid)
