#!/usr/bin/env python3
"""Deterministic bundle diff harness for M0 structural checks.

Usage:
  python scripts/diff_bundle.py --a <bundle_dir> --b <bundle_dir> [--ignore <json_pointer_or_file_rule> ...]

Notes:
  - JSON pointers use RFC-6901 syntax and support `*` per-segment wildcard.
  - File ignore rules use `file:<glob>`, for example: `--ignore file:config_effective.yaml`.
"""

from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import json
from pathlib import Path
import re
import sys
from typing import Any


VOLATILE_KEYS = {
    "timestamp",
    "timestamp_utc",
    "elapsed_sec",
    "remaining_walltime_sec",
    "walltime_sec",
}
ABS_PATH_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|/)")


class _IgnoreNode:
    pass


IGNORE_NODE = _IgnoreNode()


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _join_pointer(prefix: str, token: str) -> str:
    if not prefix:
        return "/" + _escape_pointer_token(token)
    return prefix + "/" + _escape_pointer_token(token)


def _parse_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"invalid JSON pointer (must start with '/'): {pointer!r}")
    tokens = pointer[1:].split("/")
    out: list[str] = []
    for token in tokens:
        out.append(token.replace("~1", "/").replace("~0", "~"))
    return out


def _pointer_match(path_tokens: list[str], pattern_tokens: list[str]) -> bool:
    if len(path_tokens) != len(pattern_tokens):
        return False
    for path_token, pattern_token in zip(path_tokens, pattern_tokens):
        if pattern_token == "*":
            continue
        if path_token != pattern_token:
            return False
    return True


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _looks_abs_path(value: str) -> bool:
    return bool(ABS_PATH_RE.match(value))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in sorted(value.keys()):
            if key in VOLATILE_KEYS:
                continue
            normalized_child = _normalize_value(value[key])
            out[key] = normalized_child
        return out
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, str) and _looks_abs_path(value):
        return "<ABS_PATH>"
    return value


def _apply_pointer_ignores(value: Any, ignore_patterns: list[list[str]]) -> Any:
    def _walk(node: Any, path_tokens: list[str]) -> Any:
        for pattern in ignore_patterns:
            if _pointer_match(path_tokens, pattern):
                return IGNORE_NODE

        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, child in node.items():
                child_out = _walk(child, path_tokens + [str(key)])
                if child_out is IGNORE_NODE:
                    continue
                out[key] = child_out
            return out

        if isinstance(node, list):
            out_list: list[Any] = []
            for index, child in enumerate(node):
                child_out = _walk(child, path_tokens + [str(index)])
                # Keep index alignment for list diffs.
                if child_out is IGNORE_NODE:
                    out_list.append(None)
                else:
                    out_list.append(child_out)
            return out_list

        return node

    result = _walk(value, [])
    if result is IGNORE_NODE:
        return None
    return result


def _prepare_json_payload(value: Any, ignore_patterns: list[list[str]]) -> Any:
    ignored = _apply_pointer_ignores(value, ignore_patterns)
    return _normalize_value(ignored)


def _list_files(root: Path) -> set[str]:
    files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    return files


def _is_json_file(rel_path: str) -> bool:
    return rel_path.endswith(".json")


def _is_jsonl_file(rel_path: str) -> bool:
    return rel_path.endswith(".jsonl")


def _is_yaml_file(rel_path: str) -> bool:
    return rel_path.endswith(".yaml") or rel_path.endswith(".yml")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} invalid JSON: {exc}") from exc
    return rows


def _short(value: Any, *, max_len: int = 140) -> str:
    text = repr(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _append_diff(
    diffs: list[str],
    *,
    max_diffs: int,
    rel_path: str,
    pointer: str,
    message: str,
) -> None:
    if len(diffs) >= max_diffs:
        return
    diffs.append(f"{rel_path}:{pointer or '/'}: {message}")


def _diff_values(
    a_value: Any,
    b_value: Any,
    *,
    rel_path: str,
    pointer: str,
    diffs: list[str],
    max_diffs: int,
) -> None:
    if len(diffs) >= max_diffs:
        return

    if type(a_value) is not type(b_value):  # noqa: E721
        _append_diff(
            diffs,
            max_diffs=max_diffs,
            rel_path=rel_path,
            pointer=pointer,
            message=f"type mismatch a={type(a_value).__name__} b={type(b_value).__name__}",
        )
        return

    if isinstance(a_value, dict):
        a_keys = set(a_value.keys())
        b_keys = set(b_value.keys())
        for missing in sorted(a_keys - b_keys):
            _append_diff(
                diffs,
                max_diffs=max_diffs,
                rel_path=rel_path,
                pointer=_join_pointer(pointer, missing),
                message="missing in b",
            )
            if len(diffs) >= max_diffs:
                return
        for extra in sorted(b_keys - a_keys):
            _append_diff(
                diffs,
                max_diffs=max_diffs,
                rel_path=rel_path,
                pointer=_join_pointer(pointer, extra),
                message="missing in a",
            )
            if len(diffs) >= max_diffs:
                return
        for key in sorted(a_keys & b_keys):
            _diff_values(
                a_value[key],
                b_value[key],
                rel_path=rel_path,
                pointer=_join_pointer(pointer, key),
                diffs=diffs,
                max_diffs=max_diffs,
            )
            if len(diffs) >= max_diffs:
                return
        return

    if isinstance(a_value, list):
        if len(a_value) != len(b_value):
            _append_diff(
                diffs,
                max_diffs=max_diffs,
                rel_path=rel_path,
                pointer=pointer,
                message=f"list length mismatch a={len(a_value)} b={len(b_value)}",
            )
            if len(diffs) >= max_diffs:
                return
        for index, (a_item, b_item) in enumerate(zip(a_value, b_value)):
            _diff_values(
                a_item,
                b_item,
                rel_path=rel_path,
                pointer=_join_pointer(pointer, str(index)),
                diffs=diffs,
                max_diffs=max_diffs,
            )
            if len(diffs) >= max_diffs:
                return
        return

    if a_value != b_value:
        _append_diff(
            diffs,
            max_diffs=max_diffs,
            rel_path=rel_path,
            pointer=pointer,
            message=f"value mismatch a={_short(a_value)} b={_short(b_value)}",
        )


def _detect_profile(files: set[str]) -> str:
    if {
        "config.json",
        "history_controller.jsonl",
        "param_space.yaml",
        "summary.json",
    }.issubset(files):
        return "controller_bundle"
    if {
        "controller/config.json",
        "controller/history_controller.jsonl",
        "controller/param_space.yaml",
        "controller/summary.json",
    }.issubset(files):
        return "experiment_bundle_with_controller"
    if {"config.json", "param_space.yaml", "summary.json"}.issubset(files) and (
        "history_experiment.jsonl" in files or "history.jsonl" in files
    ):
        return "experiment_bundle"
    return "generic"


def _required_for_profile(profile: str) -> tuple[set[str], list[tuple[str, str]]]:
    if profile == "controller_bundle":
        return (
            {"config.json", "history_controller.jsonl", "param_space.yaml", "summary.json"},
            [("iterations/", r"^iterations/iter_\d+\.json$")],
        )
    if profile == "experiment_bundle_with_controller":
        return (
            {
                "controller/config.json",
                "controller/history_controller.jsonl",
                "controller/param_space.yaml",
                "controller/summary.json",
            },
            [
                ("controller/iterations/", r"^controller/iterations/iter_\d+\.json$"),
                ("tasks/", r"^tasks/task_\d+_[a-zA-Z0-9_]+\.json$"),
            ],
        )
    if profile == "experiment_bundle":
        return (
            {"config.json", "param_space.yaml", "summary.json"},
            [("", r"^history(_experiment)?\.jsonl$")],
        )
    return (set(), [])


def _load_config_effective_path(root: Path, profile: str) -> str | None:
    config_rel = "config.json" if profile == "controller_bundle" or profile == "experiment_bundle" else "controller/config.json"
    config_path = root / config_rel
    if not config_path.exists():
        return None
    try:
        payload = _load_json(config_path)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("config_effective_path")
    if isinstance(raw, str) and raw.strip():
        return (Path(config_rel).parent / raw.strip()).as_posix() if Path(config_rel).parent.as_posix() != "." else raw.strip()
    return None


def _collect_semantic_values(doc: Any, *, source: str) -> tuple[list[str], list[float], list[str]]:
    run_ids: list[str] = []
    scalars: list[float] = []
    strict_statuses: list[str] = []

    def _num(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            run_id = node.get("run_id")
            if isinstance(run_id, str) and run_id:
                run_ids.append(run_id)
            run_id_cap = node.get("RUN_ID")
            if isinstance(run_id_cap, str) and run_id_cap:
                run_ids.append(run_id_cap)

            metric_obj = node.get("metric")
            if isinstance(metric_obj, dict):
                metric_scalar = _num(metric_obj.get("scalar"))
                if metric_scalar is None:
                    metric_scalar = _num(metric_obj.get("value"))
                if metric_scalar is not None:
                    scalars.append(metric_scalar)

            if node.get("tool") == "compute_metric":
                result = node.get("result")
                if isinstance(result, dict):
                    metric_scalar = _num(result.get("scalar"))
                    if metric_scalar is None:
                        metric_scalar = _num(result.get("metric_value"))
                    if metric_scalar is not None:
                        scalars.append(metric_scalar)

            if "metric_name" in node:
                metric_scalar = _num(node.get("scalar"))
                if metric_scalar is None:
                    metric_scalar = _num(node.get("metric_value"))
                if metric_scalar is not None:
                    scalars.append(metric_scalar)

            strict_replay = node.get("strict_replay")
            if isinstance(strict_replay, dict):
                status = strict_replay.get("status")
                if isinstance(status, str):
                    strict_statuses.append(status)

            replay_mode = node.get("replay_mode")
            status = node.get("status")
            if replay_mode == "strict" and isinstance(status, str):
                strict_statuses.append(status)

            for child in node.values():
                _walk(child)
            return

        if isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(doc)
    return (run_ids, scalars, strict_statuses)


def _load_semantic_docs(root: Path, files: set[str]) -> list[tuple[str, Any]]:
    docs: list[tuple[str, Any]] = []
    for rel in sorted(files):
        path = root / rel
        if _is_json_file(rel):
            try:
                docs.append((rel, _load_json(path)))
            except Exception:  # noqa: BLE001
                continue
        elif _is_jsonl_file(rel):
            try:
                rows = _load_jsonl(path)
            except Exception:  # noqa: BLE001
                continue
            for index, row in enumerate(rows):
                docs.append((f"{rel}:{index}", row))
    return docs


def _compare_counter(
    *,
    label: str,
    a_values: list[Any],
    b_values: list[Any],
    diffs: list[str],
    max_diffs: int,
) -> None:
    if len(diffs) >= max_diffs:
        return
    a_counter = Counter(a_values)
    b_counter = Counter(b_values)
    if a_counter == b_counter:
        return
    _append_diff(
        diffs,
        max_diffs=max_diffs,
        rel_path="<semantic>",
        pointer=f"/{label}",
        message=f"multiset mismatch a={dict(a_counter)} b={dict(b_counter)}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic bundle diff harness.")
    parser.add_argument("--a", required=True, type=Path, help="Bundle A directory path.")
    parser.add_argument("--b", required=True, type=Path, help="Bundle B directory path.")
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="JSON pointer ignore rule, or file:<glob> for file-level ignores.",
    )
    parser.add_argument("--max-diffs", type=int, default=20, help="Max diff lines to print on FAIL.")
    args = parser.parse_args()

    bundle_a = args.a.resolve()
    bundle_b = args.b.resolve()
    if not bundle_a.exists() or not bundle_a.is_dir():
        print(f"FAIL\n- invalid bundle dir --a: {bundle_a}")
        return 1
    if not bundle_b.exists() or not bundle_b.is_dir():
        print(f"FAIL\n- invalid bundle dir --b: {bundle_b}")
        return 1

    json_pointer_patterns: list[list[str]] = []
    file_ignores: list[str] = []
    for ignore_rule in args.ignore:
        if ignore_rule.startswith("file:"):
            file_ignores.append(ignore_rule[len("file:") :].strip())
            continue
        try:
            json_pointer_patterns.append(_parse_pointer(ignore_rule))
        except ValueError as exc:
            print(f"FAIL\n- invalid --ignore rule {ignore_rule!r}: {exc}")
            return 1

    def _is_ignored_file(rel_path: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, pattern) for pattern in file_ignores)

    diffs: list[str] = []

    files_a_raw = _list_files(bundle_a)
    files_b_raw = _list_files(bundle_b)
    files_a = {path for path in files_a_raw if not _is_ignored_file(path)}
    files_b = {path for path in files_b_raw if not _is_ignored_file(path)}

    profile_a = _detect_profile(files_a_raw)
    profile_b = _detect_profile(files_b_raw)
    if profile_a != profile_b:
        _append_diff(
            diffs,
            max_diffs=args.max_diffs,
            rel_path="<profile>",
            pointer="/",
            message=f"profile mismatch a={profile_a} b={profile_b}",
        )

    required_files, required_patterns = _required_for_profile(profile_a)
    for required in sorted(required_files):
        if _is_ignored_file(required):
            continue
        if required not in files_a:
            _append_diff(
                diffs,
                max_diffs=args.max_diffs,
                rel_path="<required>",
                pointer="/" + required,
                message="missing in a",
            )
        if required not in files_b:
            _append_diff(
                diffs,
                max_diffs=args.max_diffs,
                rel_path="<required>",
                pointer="/" + required,
                message="missing in b",
            )

    for prefix, pattern in required_patterns:
        if prefix and _is_ignored_file(prefix.rstrip("/") + "/*"):
            continue
        regex = re.compile(pattern)
        a_matches = [path for path in files_a if regex.match(path)]
        b_matches = [path for path in files_b if regex.match(path)]
        if not a_matches:
            _append_diff(
                diffs,
                max_diffs=args.max_diffs,
                rel_path="<required>",
                pointer="/" + prefix,
                message=f"missing required pattern in a: {pattern}",
            )
        if not b_matches:
            _append_diff(
                diffs,
                max_diffs=args.max_diffs,
                rel_path="<required>",
                pointer="/" + prefix,
                message=f"missing required pattern in b: {pattern}",
            )

    cfg_eff_a = _load_config_effective_path(bundle_a, profile_a)
    cfg_eff_b = _load_config_effective_path(bundle_b, profile_b)
    if cfg_eff_a and not _is_ignored_file(cfg_eff_a) and cfg_eff_a not in files_a:
        _append_diff(
            diffs,
            max_diffs=args.max_diffs,
            rel_path="<required>",
            pointer="/" + cfg_eff_a,
            message="config_effective_path points to missing file in a",
        )
    if cfg_eff_b and not _is_ignored_file(cfg_eff_b) and cfg_eff_b not in files_b:
        _append_diff(
            diffs,
            max_diffs=args.max_diffs,
            rel_path="<required>",
            pointer="/" + cfg_eff_b,
            message="config_effective_path points to missing file in b",
        )

    for missing_in_b in sorted(files_a - files_b):
        _append_diff(
            diffs,
            max_diffs=args.max_diffs,
            rel_path="<tree>",
            pointer="/" + missing_in_b,
            message="present in a only",
        )
    for missing_in_a in sorted(files_b - files_a):
        _append_diff(
            diffs,
            max_diffs=args.max_diffs,
            rel_path="<tree>",
            pointer="/" + missing_in_a,
            message="present in b only",
        )

    common_files = sorted(files_a & files_b)
    for rel in common_files:
        if len(diffs) >= args.max_diffs:
            break

        a_path = bundle_a / rel
        b_path = bundle_b / rel

        if _is_yaml_file(rel):
            a_bytes = a_path.read_bytes()
            b_bytes = b_path.read_bytes()
            if a_bytes != b_bytes:
                _append_diff(
                    diffs,
                    max_diffs=args.max_diffs,
                    rel_path=rel,
                    pointer="/",
                    message="byte mismatch",
                )
            continue

        if _is_json_file(rel):
            try:
                a_obj = _load_json(a_path)
                b_obj = _load_json(b_path)
            except Exception as exc:  # noqa: BLE001
                _append_diff(
                    diffs,
                    max_diffs=args.max_diffs,
                    rel_path=rel,
                    pointer="/",
                    message=f"JSON parse error: {exc}",
                )
                continue

            a_prepared = _prepare_json_payload(a_obj, json_pointer_patterns)
            b_prepared = _prepare_json_payload(b_obj, json_pointer_patterns)
            if _canonical_json(a_prepared) != _canonical_json(b_prepared):
                _diff_values(
                    a_prepared,
                    b_prepared,
                    rel_path=rel,
                    pointer="",
                    diffs=diffs,
                    max_diffs=args.max_diffs,
                )
            continue

        if _is_jsonl_file(rel):
            try:
                a_rows = _load_jsonl(a_path)
                b_rows = _load_jsonl(b_path)
            except Exception as exc:  # noqa: BLE001
                _append_diff(
                    diffs,
                    max_diffs=args.max_diffs,
                    rel_path=rel,
                    pointer="/",
                    message=f"JSONL parse error: {exc}",
                )
                continue

            if len(a_rows) != len(b_rows):
                _append_diff(
                    diffs,
                    max_diffs=args.max_diffs,
                    rel_path=rel,
                    pointer="/",
                    message=f"line count mismatch a={len(a_rows)} b={len(b_rows)}",
                )
                continue

            for index, (a_row, b_row) in enumerate(zip(a_rows, b_rows)):
                if len(diffs) >= args.max_diffs:
                    break
                a_prepared = _prepare_json_payload(a_row, json_pointer_patterns)
                b_prepared = _prepare_json_payload(b_row, json_pointer_patterns)
                if _canonical_json(a_prepared) == _canonical_json(b_prepared):
                    continue
                _diff_values(
                    a_prepared,
                    b_prepared,
                    rel_path=rel,
                    pointer="/" + str(index),
                    diffs=diffs,
                    max_diffs=args.max_diffs,
                )

    docs_a = _load_semantic_docs(bundle_a, files_a)
    docs_b = _load_semantic_docs(bundle_b, files_b)
    run_ids_a: list[str] = []
    run_ids_b: list[str] = []
    scalars_a: list[float] = []
    scalars_b: list[float] = []
    strict_statuses_a: list[str] = []
    strict_statuses_b: list[str] = []

    for source, doc in docs_a:
        r, s, st = _collect_semantic_values(doc, source=source)
        run_ids_a.extend(r)
        scalars_a.extend(s)
        strict_statuses_a.extend(st)
    for source, doc in docs_b:
        r, s, st = _collect_semantic_values(doc, source=source)
        run_ids_b.extend(r)
        scalars_b.extend(s)
        strict_statuses_b.extend(st)

    _compare_counter(
        label="run_id",
        a_values=sorted(run_ids_a),
        b_values=sorted(run_ids_b),
        diffs=diffs,
        max_diffs=args.max_diffs,
    )
    _compare_counter(
        label="metric_scalar",
        a_values=sorted(scalars_a),
        b_values=sorted(scalars_b),
        diffs=diffs,
        max_diffs=args.max_diffs,
    )
    _compare_counter(
        label="strict_replay_status",
        a_values=sorted(strict_statuses_a),
        b_values=sorted(strict_statuses_b),
        diffs=diffs,
        max_diffs=args.max_diffs,
    )

    if diffs:
        print("FAIL")
        for diff in diffs[: args.max_diffs]:
            print(f"- {diff}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
