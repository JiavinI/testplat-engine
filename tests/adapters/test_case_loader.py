"""Case loader / validator 契约：G1–G6 加载与 M1–M8 拒绝。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from testplat_engine.adapters.filesystem.case_loader import (
    case_id_to_path,
    load_case,
    parse_case_dict,
    validate_case_request_params,
)
from testplat_engine.adapters.filesystem.registry import Registry
from testplat_engine.interface import (
    AssetNotFoundError,
    InvalidAssetError,
    InvalidInputError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "assets"


def test_case_id_to_path_canonical_mapping() -> None:
    path = case_id_to_path("case.demo.example", FIXTURES)
    assert path == (FIXTURES / "scenarios" / "demo" / "example.case.yaml").resolve()


def test_g1_success_loads_with_stable_order() -> None:
    case = load_case("case.demo.g1_success", FIXTURES)
    assert case.id == "case.demo.g1_success"
    assert case.schema_version == "0.2"
    assert [s.step_id for s in case.steps] == ["create", "get"]
    assert case.steps[0].output == "created"
    assert case.steps[1].output is None
    assert "created.body.id" in case.steps[1].params["resource_id"]
    merged = validate_case_request_params(case, {})
    assert merged["resource_name"] == "demo"


def test_g2_g3_g4b_g6_load_ok() -> None:
    for case_id, step_count in [
        ("case.demo.g2_api_assert_failed", 2),
        ("case.demo.g3_typed_propagation", 3),
        ("case.demo.g4b_missing_runtime_path", 3),
        ("case.demo.g6a_timeout", 3),
        ("case.demo.g6b_transport", 3),
    ]:
        case = load_case(case_id, FIXTURES)
        assert len(case.steps) == step_count
        assert case.id == case_id


def test_g4a_undeclared_alias_invalid_asset() -> None:
    with pytest.raises(InvalidAssetError) as exc_info:
        load_case("case.demo.g4a_undeclared_alias", FIXTURES)
    err = exc_info.value
    assert err.kind == "invalid_asset"
    assert err.stage == "preflight"
    assert "alias" in err.public_message.lower() or "未声明" in err.public_message


def test_g5_unsupported_step_type_invalid_asset() -> None:
    with pytest.raises(InvalidAssetError) as exc_info:
        load_case("case.demo.g5_unsupported_step", FIXTURES)
    err = exc_info.value
    assert err.kind == "invalid_asset"
    assert err.stage == "preflight"
    assert "ui" in err.public_message


def test_m1_duplicate_step_id() -> None:
    data = {
        "schema_version": "0.2",
        "id": "case.demo.m1",
        "steps": [
            {"step_id": "dup", "type": "api", "api_id": "api.demo.get"},
            {"step_id": "dup", "type": "api", "api_id": "api.demo.get"},
        ],
    }
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(data)
    assert ei.value.kind == "invalid_asset"
    assert ei.value.stage == "preflight"
    assert "step_id" in ei.value.public_message


def test_m2_duplicate_output() -> None:
    data = {
        "schema_version": "0.2",
        "id": "case.demo.m2",
        "steps": [
            {"step_id": "a", "type": "api", "api_id": "api.demo.get", "output": "x"},
            {"step_id": "b", "type": "api", "api_id": "api.demo.get", "output": "x"},
        ],
    }
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(data)
    assert ei.value.kind == "invalid_asset"
    assert "output" in ei.value.public_message


def test_m3_forward_reference() -> None:
    data = {
        "schema_version": "0.2",
        "id": "case.demo.m3",
        "steps": [
            {
                "step_id": "a",
                "type": "api",
                "api_id": "api.demo.get",
                "params": {"id": "${later.body.id}"},
            },
            {"step_id": "b", "type": "api", "api_id": "api.demo.get", "output": "later"},
        ],
    }
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(data)
    assert ei.value.kind == "invalid_asset"
    assert ei.value.stage == "preflight"


def test_m4_unknown_fields_rejected() -> None:
    with pytest.raises(InvalidAssetError) as top:
        parse_case_dict(
            {
                "schema_version": "0.2",
                "id": "case.demo.m4",
                "metadata": {"owner": "x"},
                "steps": [{"step_id": "a", "type": "api", "api_id": "api.demo.get"}],
            }
        )
    assert top.value.kind == "invalid_asset"
    assert "metadata" in top.value.public_message

    with pytest.raises(InvalidAssetError) as step:
        parse_case_dict(
            {
                "schema_version": "0.2",
                "id": "case.demo.m4b",
                "steps": [
                    {
                        "step_id": "a",
                        "type": "api",
                        "api_id": "api.demo.get",
                        "assert": {"status_code": 200},
                        "kind": "api",
                    }
                ],
            }
        )
    assert step.value.kind == "invalid_asset"
    msg = step.value.public_message
    assert "assert" in msg or "kind" in msg


def test_m5_omit_output_is_valid() -> None:
    case = load_case("case.demo.m5_omit_output", FIXTURES)
    assert case.steps[0].output is None


def test_m6_steps_index_ref_rejected() -> None:
    data = {
        "schema_version": "0.2",
        "id": "case.demo.m6",
        "steps": [
            {
                "step_id": "a",
                "type": "api",
                "api_id": "api.demo.get",
                "params": {"id": "${steps.0.response.body.id}"},
            }
        ],
    }
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(data)
    assert ei.value.kind == "invalid_asset"
    assert "下标" in ei.value.public_message or "steps" in ei.value.public_message


def test_m7_file_id_mismatch() -> None:
    raw = yaml.safe_load(
        (FIXTURES / "scenarios" / "demo" / "g1_success.case.yaml").read_text(encoding="utf-8")
    )
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(raw, expected_case_id="case.demo.other")
    assert ei.value.kind == "invalid_asset"
    assert ei.value.stage == "preflight"
    assert "不一致" in ei.value.public_message


def test_m8_missing_params_invalid_input() -> None:
    case = load_case("case.demo.m8_missing_params", FIXTURES)
    with pytest.raises(InvalidInputError) as ei:
        validate_case_request_params(case, {})
    err = ei.value
    assert err.kind == "invalid_input"
    assert err.stage == "preflight"
    assert "params.missing" in err.public_message


def test_m8_missing_params_satisfied_by_request_override() -> None:
    case = load_case("case.demo.m8_missing_params", FIXTURES)
    merged = validate_case_request_params(case, {"missing": "ok"})
    assert merged["missing"] == "ok"


def test_asset_not_found() -> None:
    with pytest.raises(AssetNotFoundError) as ei:
        load_case("case.demo.does_not_exist", FIXTURES)
    assert ei.value.kind == "asset_not_found"
    assert ei.value.stage == "preflight"


def test_registry_load_case() -> None:
    registry = Registry(FIXTURES)
    case = registry.load_case("case.demo.g1_success")
    assert case.steps[0].api_id == "api.demo.create"


def test_rejects_old_aliases_use_with_arrow() -> None:
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(
            {
                "schema_version": "0.2",
                "id": "case.demo.old",
                "steps": [
                    {
                        "step_id": "a",
                        "type": "api",
                        "api_id": "api.demo.get",
                        "use": "api.demo.get",
                        "with": {},
                        "->": "alias",
                    }
                ],
            }
        )
    assert ei.value.kind == "invalid_asset"


def test_self_reference_via_own_output_is_forward() -> None:
    """步骤引用自己的 output 属于前向/自引用，必须拒绝。"""
    data = {
        "schema_version": "0.2",
        "id": "case.demo.self",
        "steps": [
            {
                "step_id": "a",
                "type": "api",
                "api_id": "api.demo.get",
                "params": {"id": "${mine.body.id}"},
                "output": "mine",
            }
        ],
    }
    with pytest.raises(InvalidAssetError):
        parse_case_dict(data)


# 运行期 G1–G6 / M8 的真实结果由 tests/engine/test_case_golden.py 经公开
# execute(CaseExecutionRequest) 验证；此处只保留加载契约（不再有 pending 占位）。


# --- S2 / S3 / P2 / P4：loader 修复回归 ---


def _minimal_case(**overrides):
    data = {
        "schema_version": "0.2",
        "id": "case.demo.fix",
        "steps": [{"step_id": "a", "type": "api", "api_id": "api.demo.get"}],
    }
    data.update(overrides)
    return data


def test_s2_non_string_yaml_keys_invalid_asset_not_typeerror() -> None:
    """S2：非字符串键不得 TypeError；顶层与 step 均 invalid_asset/preflight。"""
    with pytest.raises(InvalidAssetError) as top:
        parse_case_dict(
            {
                "schema_version": "0.2",
                "id": "case.demo.s2",
                123: "bad",
                "steps": [{"step_id": "a", "type": "api", "api_id": "api.demo.get"}],
            }
        )
    assert top.value.kind == "invalid_asset"
    assert top.value.stage == "preflight"
    assert "字符串" in top.value.public_message

    with pytest.raises(InvalidAssetError) as mixed:
        parse_case_dict(
            {
                "schema_version": "0.2",
                "id": "case.demo.s2b",
                "metadata": {"x": 1},
                7: "also",
                "steps": [{"step_id": "a", "type": "api", "api_id": "api.demo.get"}],
            }
        )
    assert mixed.value.kind == "invalid_asset"
    assert mixed.value.stage == "preflight"

    with pytest.raises(InvalidAssetError) as step:
        parse_case_dict(
            {
                "schema_version": "0.2",
                "id": "case.demo.s2c",
                "steps": [
                    {
                        "step_id": "a",
                        "type": "api",
                        "api_id": "api.demo.get",
                        9: "nope",
                    }
                ],
            }
        )
    assert step.value.kind == "invalid_asset"
    assert step.value.stage == "preflight"
    assert "字符串" in step.value.public_message


@pytest.mark.parametrize(
    "raw,needle",
    [
        ("${params.a", "未闭合"),
        ("prefix ${params.a", "未闭合"),
        ("${}", "空"),
        ("x=${}y", "空"),
        ("${{params.a}}", "花括号"),
        ("${params.{a}}", "花括号"),
        ("${params.a{b}}", "花括号"),
    ],
)
def test_s3_placeholder_syntax_mutations_rejected(raw: str, needle: str) -> None:
    """S3：未闭合 / 空 / 多余花括号整串扫描拒绝，禁止静默降级。"""
    data = _minimal_case(
        steps=[
            {
                "step_id": "a",
                "type": "api",
                "api_id": "api.demo.get",
                "params": {"v": raw},
            }
        ]
    )
    with pytest.raises(InvalidAssetError) as ei:
        parse_case_dict(data)
    assert ei.value.kind == "invalid_asset"
    assert ei.value.stage == "preflight"
    assert needle in ei.value.public_message


def test_s3_wellformed_placeholder_still_accepted() -> None:
    case = parse_case_dict(
        _minimal_case(
            params={"resource_name": "demo"},
            steps=[
                {
                    "step_id": "a",
                    "type": "api",
                    "api_id": "api.demo.get",
                    "params": {
                        "name": "${params.resource_name}",
                        "label": "x-${params.resource_name}-y",
                    },
                }
            ],
        )
    )
    assert "params.resource_name" in case.steps[0].params["name"]


def test_p2_params_full_path_walk_missing_and_non_object() -> None:
    """P2：${params.a.b.c} 全路径行走；缺失或中途非对象 → invalid_input + 完整路径。"""
    case_ok = parse_case_dict(
        _minimal_case(
            params={"a": {"b": {"c": 1}}},
            steps=[
                {
                    "step_id": "a",
                    "type": "api",
                    "api_id": "api.demo.get",
                    "params": {"v": "${params.a.b.c}"},
                }
            ],
        )
    )
    assert validate_case_request_params(case_ok, {})["a"]["b"]["c"] == 1

    case_missing = parse_case_dict(
        _minimal_case(
            params={"a": {"b": {}}},
            steps=[
                {
                    "step_id": "a",
                    "type": "api",
                    "api_id": "api.demo.get",
                    "params": {"v": "${params.a.b.c}"},
                }
            ],
        )
    )
    with pytest.raises(InvalidInputError) as miss:
        validate_case_request_params(case_missing, {})
    assert miss.value.kind == "invalid_input"
    assert miss.value.stage == "preflight"
    assert "params.a.b.c" in miss.value.public_message

    case_scalar = parse_case_dict(
        _minimal_case(
            params={"a": "only-string"},
            steps=[
                {
                    "step_id": "a",
                    "type": "api",
                    "api_id": "api.demo.get",
                    "params": {"v": "${params.a.b.c}"},
                }
            ],
        )
    )
    with pytest.raises(InvalidInputError) as mid:
        validate_case_request_params(case_scalar, {})
    assert mid.value.kind == "invalid_input"
    assert mid.value.stage == "preflight"
    assert "params.a.b.c" in mid.value.public_message


def test_p4_explicit_null_rejected_missing_defaults() -> None:
    """P4：缺省用默认；显式 null 拒绝（顶层 tags/params 与 step params）。"""
    ok = parse_case_dict(_minimal_case())
    assert ok.tags == ()
    assert ok.params == {}
    assert ok.steps[0].params == {}

    with pytest.raises(InvalidAssetError) as tags:
        parse_case_dict(_minimal_case(tags=None))
    assert tags.value.kind == "invalid_asset"
    assert tags.value.stage == "preflight"
    assert "tags" in tags.value.public_message
    assert "null" in tags.value.public_message

    with pytest.raises(InvalidAssetError) as params:
        parse_case_dict(_minimal_case(params=None))
    assert params.value.kind == "invalid_asset"
    assert "params" in params.value.public_message
    assert "null" in params.value.public_message

    with pytest.raises(InvalidAssetError) as step_params:
        parse_case_dict(
            _minimal_case(
                steps=[
                    {
                        "step_id": "a",
                        "type": "api",
                        "api_id": "api.demo.get",
                        "params": None,
                    }
                ]
            )
        )
    assert step_params.value.kind == "invalid_asset"
    assert step_params.value.stage == "preflight"
    assert "params" in step_params.value.public_message
    assert "null" in step_params.value.public_message
