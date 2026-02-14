import json

import cholla_utils


def test_get_source_input_paths_prefers_canonical():
    manifest = {
        "source_params_path": "/canonical/params.txt",
        "source_schedule_path": "/canonical/scale_outputs.txt",
        "params_source_path": "/legacy/params.txt",
        "schedule_source_path": "/legacy/scale_outputs.txt",
    }

    source_params, source_schedule = cholla_utils.get_source_input_paths(manifest)
    assert source_params == "/canonical/params.txt"
    assert source_schedule == "/canonical/scale_outputs.txt"


def test_get_source_input_paths_falls_back_to_legacy():
    manifest = {
        "params_source_path": "/legacy/params.txt",
        "schedule_source_path": "/legacy/scale_outputs.txt",
    }

    source_params, source_schedule = cholla_utils.get_source_input_paths(manifest)
    assert source_params == "/legacy/params.txt"
    assert source_schedule == "/legacy/scale_outputs.txt"


def test_get_source_input_paths_fallback_when_canonical_blank():
    manifest = {
        "source_params_path": "  ",
        "source_schedule_path": "",
        "params_source_path": "/legacy/params.txt",
        "schedule_source_path": "/legacy/scale_outputs.txt",
    }

    source_params, source_schedule = cholla_utils.get_source_input_paths(manifest)
    assert source_params == "/legacy/params.txt"
    assert source_schedule == "/legacy/scale_outputs.txt"


def test_load_run_manifest_reads_json_object(tmp_path):
    manifest_path = tmp_path / "run_manifest.json"
    payload = {
        "source_params_path": "/canonical/params.txt",
        "source_schedule_path": "/canonical/scale_outputs.txt",
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = cholla_utils.load_run_manifest(manifest_path)
    assert loaded == payload
