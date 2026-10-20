"""新版场景通过公开入口验证；本机假服务不产生真实业务。"""

import json
import shutil

import pytest
import yaml

from testplat_engine import (
    CaseExecutionRequest,
    EngineTimeoutError,
    InvalidAssetError,
    InvalidInputError,
    SingleApiEngine,
    describe_case,
)
from testplat_engine.engine.scenario_assert import evaluate
from tests.engine.case_support import copy_assets, start_server, stop_server


@pytest.fixture
def scenario(tmp_path):
    root = copy_assets(tmp_path)
    flow = {
        "schema_version": "0.3",
        "id": "flow.demo.echo",
        "inputs": {"value": {"type": "object"}},
        "outputs": {"value": "${echo.body.echo}"},
        "steps": [
            {
                "step_id": "echo",
                "type": "api",
                "api_id": "api.demo.echo_object",
                "params": {"value": "${inputs.value}"},
                "output": "echo",
            }
        ],
    }
    case = {
        "schema_version": "0.3",
        "id": "case.demo.scenario",
        "params": {"common": {"flag": True}, "private": {"first": 1, "second": 2}},
        "steps": [
            {
                "step_id": name,
                "type": "flow",
                "flow_id": "flow.demo.echo",
                "params": {
                    "value": {
                        "shared": "${params.common.flag}",
                        "own": "${params.private." + name + "}",
                    }
                },
                "output": name,
            }
            for name in ("first", "second")
        ]
        + [
            {
                "step_id": "consume",
                "type": "api",
                "api_id": "api.demo.echo_object",
                "params": {"value": {"a": "${first.value}", "b": "${second.value}"}},
            }
        ],
    }

    def save():
        for doc, suffix in ((case, "case"), (flow, "flow")):
            (root / "scenarios/demo" / f"{doc['id'].split('.')[-1]}.{suffix}.yaml").write_text(
                yaml.safe_dump(doc)
            )

    save()
    return root, case, flow, save


@pytest.fixture
def server():
    srv, thread, state, base = start_server()
    try:
        yield state, base
    finally:
        stop_server(srv, thread, state)


def request(root, base, params=None):
    return CaseExecutionRequest(
        "case.demo.scenario",
        root,
        {"api_hosts": {"local": base}},
        params=params or {},
        timeout_seconds=0.1,
    )


async def test_repeated_flow_isolated_typed_outputs_and_description(scenario, server):
    root, _, _, _ = scenario
    state, base = server
    description = describe_case("case.demo.scenario", root)
    assert description["steps"][0]["definition"]["inputs"] == {"value": {"type": "object"}}
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["result_version"] == "0.3"
    assert result["status"] == "success"
    assert [r.body["value"] for r in state.requests[:2]] == [
        {"shared": True, "own": 1},
        {"shared": True, "own": 2},
    ]
    assert state.requests[2].body["value"]["a"]["own"] == 1
    assert state.requests[2].body["value"]["b"]["own"] == 2
    first, second, consume = result["steps"]
    assert first["trace"]["node_id"] != second["trace"]["node_id"]
    assert first["trace"]["children"][0]["trace"]["parent_id"] == first["trace"]["node_id"]
    assert consume["trace"]["bindings"][0]["source_node_id"] == first["trace"]["node_id"]
    assert consume["trace"]["bindings"][0]["value_type"] == "object"
    json.dumps(result)


async def test_ordered_map_runs_flow_per_item_with_typed_index_and_nested_trace(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    flow["inputs"] = {
        "value": {"type": "object"},
        "index": {"type": "integer"},
    }
    flow["steps"][0]["params"]["value"] = {
        "item": "${inputs.value}",
        "index": "${inputs.index}",
    }
    case["params"] = {"private": {"items": [{"quantity": 3}, {"quantity": 1}]}}
    case["steps"] = [
        {
            "step_id": "resolve_items",
            "type": "map",
            "flow_id": flow["id"],
            "source": "${params.private.items}",
            "params": {"value": "${item}", "index": "${index}"},
            "output": "resolved",
        }
    ]
    save()

    result = (await SingleApiEngine().execute(request(root, base))).to_dict()

    assert result["status"] == "success"
    assert [r.body["value"] for r in state.requests] == [
        {"item": {"quantity": 3}, "index": 0},
        {"item": {"quantity": 1}, "index": 1},
    ]
    mapped = result["steps"][0]
    assert mapped["type"] == "map"
    assert [child["trace"]["inputs"]["index"] for child in mapped["trace"]["children"]] == [0, 1]
    assert all(
        child["trace"]["children"][0]["trace"]["parent_id"] == child["trace"]["node_id"]
        for child in mapped["trace"]["children"]
    )
    assert mapped["trace"]["outputs"] == [
        {"value": {"item": {"quantity": 3}, "index": 0}},
        {"value": {"item": {"quantity": 1}, "index": 1}},
    ]


async def test_map_rejects_empty_input_before_any_request(scenario, server):
    root, case, _, save = scenario
    state, base = server
    case["params"] = {"private": {"items": []}}
    case["steps"] = [
        {
            "step_id": "resolve_items",
            "type": "map",
            "flow_id": "flow.demo.echo",
            "source": "${params.private.items}",
            "params": {"value": "${item}"},
        }
    ]
    save()

    with pytest.raises(InvalidInputError):
        await SingleApiEngine().execute(request(root, base))

    assert state.requests == []


async def test_map_source_from_prior_response_is_checked_before_iteration_requests(
    scenario, server
):
    root, case, flow, save = scenario
    state, base = server
    flow["outputs"] = {"value": "${echo.body.echo}"}
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": [{"ok": True}]},
            "output": "fetch",
        },
        {
            "step_id": "resolve_items",
            "type": "map",
            "flow_id": flow["id"],
            "source": "${fetch.body.echo}",
            "params": {"value": "${item}"},
            "output": "resolved",
        },
    ]
    save()

    result = (await SingleApiEngine().execute(request(root, base))).to_dict()

    assert result["status"] == "success"
    assert len(state.requests) == 2
    assert state.requests[1].body["value"] == {"ok": True}
    assert (
        result["steps"][1]["trace"]["bindings"][0]["source_node_id"]
        == result["steps"][0]["trace"]["node_id"]
    )


async def test_map_timeout_preserves_iteration_trace_and_skips_remaining_items(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    flow["steps"][0]["api_id"] = "api.demo.timeout_target"
    case["params"] = {"private": {"items": [{"id": 1}, {"id": 2}]}}
    case["steps"] = [
        {
            "step_id": "resolve_items",
            "type": "map",
            "flow_id": flow["id"],
            "source": "${params.private.items}",
            "params": {"value": "${item}"},
            "output": "resolved",
        }
    ]
    save()

    with pytest.raises(EngineTimeoutError) as caught:
        await SingleApiEngine().execute(request(root, base))

    mapped = caught.value.partial_outcome.to_dict()["steps"][0]
    iterations = mapped["trace"]["children"]
    assert [item["status"] for item in iterations] == ["failed", "skipped"]
    assert iterations[0]["trace"]["iteration_index"] == 0
    assert (
        iterations[0]["trace"]["children"][0]["trace"]["parent_id"]
        == iterations[0]["trace"]["node_id"]
    )
    assert len(state.requests) == 1


@pytest.mark.parametrize("issue", ["missing", "type", "cycle", "scope", "secret", "late_api"])
async def test_preflight_zero_requests(scenario, server, issue):
    root, case, flow, save = scenario
    state, base = server
    expected = InvalidAssetError
    if issue == "missing":
        del case["params"]["private"]
        expected = InvalidInputError
    elif issue == "type":
        case["steps"][0]["params"]["value"] = 4
        expected = InvalidInputError
    elif issue == "cycle":
        flow["steps"] = [
            {
                "step_id": "loop",
                "type": "flow",
                "flow_id": flow["id"],
                "params": {"value": "${inputs.value}"},
            }
        ]
    elif issue == "scope":
        flow["steps"][0]["params"]["value"] = "${params.common}"
    elif issue == "secret":
        case["params"]["password"] = "never-persist-this"
    elif issue == "late_api":
        case["steps"][-1]["api_id"] = "api.demo.missing"
        from testplat_engine import AssetNotFoundError

        expected = AssetNotFoundError
    save()
    with pytest.raises(expected):
        await SingleApiEngine().execute(request(root, base))
    assert state.requests == []


async def test_dynamic_nested_flow_preflight_and_failure_exports(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["steps"][1]["params"]["value"] = "${first.value}"
    save()
    result = await SingleApiEngine().execute(request(root, base))
    assert result.status == "success"
    assert state.requests[1].body == state.requests[0].body
    state.requests.clear()
    flow["outputs"]["value"] = "${echo.body.missing}"
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert [s["status"] for s in result["steps"]] == ["failed", "skipped", "skipped"]
    assert result["steps"][0]["trace"]["children"][0]["status"] == "success"
    assert result["steps"][0]["error"]["kind"] == "variable_resolution"
    assert len(state.requests) == 1


async def test_nested_timeout_preserves_children_and_skips(scenario, server):
    root, _, flow, save = scenario
    state, base = server
    flow["steps"][0]["api_id"] = "api.demo.timeout_target"
    save()
    with pytest.raises(EngineTimeoutError) as caught:
        await SingleApiEngine().execute(request(root, base))
    result = caught.value.partial_outcome.to_dict()
    assert result["steps"][0]["trace"]["children"][0]["error"]["kind"] == "timeout"
    assert result["steps"][1]["status"] == "skipped"
    assert len(state.requests) == 1


def test_flow_cannot_be_run_directly(scenario):
    root, _, _, _ = scenario
    with pytest.raises(InvalidAssetError):
        describe_case("flow.demo.echo", root)


@pytest.mark.parametrize("login_fails", [False, True])
async def test_recursive_flow_login_pool_redaction_and_failure(scenario, login_fails):
    from tests.engine.login_support import (
        ENV,
        LOGIN_ASSETS,
        FakeTarget,
        assert_no_secrets,
        buyer_material,
        engine_for,
        table,
    )

    root, case, flow, save = scenario
    shutil.copytree(LOGIN_ASSETS / "api/login", root / "api/login")
    flow["steps"] = [
        {"step_id": "echo", "type": "api", "api_id": "api.login.buyer_echo", "output": "echo"},
        {"step_id": "again", "type": "api", "api_id": "api.login.buyer_echo"},
    ]
    flow["outputs"] = {"value": "${echo.body}"}
    wrapper = {
        "schema_version": "0.3",
        "id": "flow.demo.wrapper",
        "inputs": flow["inputs"],
        "outputs": {"value": "${inner.value}"},
        "steps": [
            {
                "step_id": "inner",
                "type": "flow",
                "flow_id": flow["id"],
                "params": {"value": "${inputs.value}"},
                "output": "inner",
            }
        ],
    }
    case["steps"] = case["steps"][:2]
    for step in case["steps"]:
        step["flow_id"] = wrapper["id"]
    save()
    (root / "scenarios/demo/wrapper.flow.yaml").write_text(yaml.safe_dump(wrapper))
    target = FakeTarget()
    auth = table(buyer=buyer_material(path="/login/fail")) if login_fails else table()
    result = (
        await engine_for(target).execute(
            CaseExecutionRequest(
                case["id"],
                root,
                ENV,
                params={},
                auth=auth,
            )
        )
    ).to_dict()
    inner = result["steps"][0]["trace"]["children"][0]
    leaves = inner["trace"]["children"]
    assert leaves[0]["trace"]["parent_id"] == inner["trace"]["node_id"]
    if login_fails:
        assert result["status"] == "failed"
        assert leaves[1]["skip_reason"] == "login_failed"
        assert result["steps"][1]["skip_reason"] == "login_failed"
        assert len(target.requests) == 1
    else:
        assert result["status"] == "success"
        assert target.paths().count("/login/json") == 1
        assert target.paths().count("/echo/token") == 4
        assert len(result["logins"]) == 1
    assert_no_secrets(target, result)


def extraction(source="${params.rows}", **overrides):
    return {
        "step_id": "choose",
        "type": "extract",
        "source": source,
        "selector": {"path": [], "where": [{"path": ["code"], "equals": "${params.code}"}]},
        "cardinality": "one",
        "value_type": "object",
        "output": "chosen",
        **overrides,
    }


async def test_extract_exact_match_dynamic_source_description_and_provenance(scenario, server):
    root, case, _, save = scenario
    state, base = server
    # The condition is data, including characters that would be JSONPath syntax.
    code = "x'] || @.code != 'y"
    case["params"] = {"code": code}
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": {"rows": [{"code": "other"}, {"code": code, "qty": 0}]}},
            "output": "fetch",
        },
        extraction(
            "${fetch.body.echo}",
            selector={"path": ["rows"], "where": [{"path": ["code"], "equals": "${params.code}"}]},
        ),
        {
            "step_id": "consume",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": "${chosen}"},
        },
    ]
    save()
    described = describe_case(case["id"], root)["steps"][1]
    assert described["cardinality"] == "one"
    assert described["selector"]["path"] == ["rows"]
    assert state.requests == []
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert len(state.requests) == 2
    assert state.requests[-1].body["value"] == {"code": code, "qty": 0}
    trace = result["steps"][1]["trace"]
    assert trace["extracts"][0]["match_count"] == 1
    assert trace["bindings"][0]["source_node_id"] == result["steps"][0]["trace"]["node_id"]
    assert result["steps"][2]["trace"]["bindings"][0]["source_node_id"] == trace["node_id"]
    json.dumps(result)


@pytest.mark.parametrize("rows,count", [([], 0), ([{"code": "a"}, {"code": "a"}], 2)])
async def test_extract_dynamic_cardinality_failure_stops_without_publishing(
    scenario, server, rows, count
):
    root, case, _, save = scenario
    state, base = server
    case["params"] = {"code": "a"}
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": rows},
            "output": "fetch",
        },
        extraction("${fetch.body.echo}"),
        {
            "step_id": "consume",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": "${chosen}"},
        },
    ]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert [s["status"] for s in result["steps"]] == ["success", "failed", "skipped"]
    failed = result["steps"][1]
    assert failed["error"]["kind"] == "variable_resolution"
    assert failed["trace"]["extracts"][0]["match_count"] == count
    assert failed["trace"]["outputs"] == {}
    assert len(state.requests) == 1


@pytest.mark.parametrize(
    "value,kind,nullable",
    [
        (0, "integer", False),
        (False, "boolean", False),
        ([], "array", False),
        (None, "object", True),
        (None, "null", False),
    ],
)
async def test_extract_index_preserves_falsy_and_null(scenario, server, value, kind, nullable):
    root, case, _, save = scenario
    _, base = server
    case["params"] = {"rows": [{"value": value}]}
    case["steps"] = [
        extraction(selector={"path": [0, "value"]}, value_type=kind, nullable=nullable)
    ]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert result["steps"][0]["trace"]["outputs"] == value


@pytest.mark.parametrize(
    "rows,selector,kind",
    [
        ([], {"path": [0]}, "object"),
        ([{}], {"path": [0, "missing"]}, "null"),
        ([{"value": None}], {"path": [0, "value"]}, "object"),
        ([{"code": True}], {"path": [], "where": [{"path": ["code"], "equals": 1}]}, "object"),
        ([{"code": "1"}], {"path": [], "where": [{"path": ["code"], "equals": 1}]}, "object"),
        ([False], {"path": [0]}, "integer"),
    ],
)
async def test_extract_known_invalid_data_zero_requests(scenario, server, rows, selector, kind):
    root, case, _, save = scenario
    state, base = server
    case["params"] = {"rows": rows}
    case["steps"] = [extraction(selector=selector, value_type=kind), case["steps"][-1]]
    case["steps"][-1]["params"] = {"value": "${chosen}"}
    save()
    with pytest.raises(InvalidInputError):
        await SingleApiEngine().execute(request(root, base))
    assert state.requests == []


@pytest.mark.parametrize(
    "change",
    [
        {"selector": {"path": [-1]}},
        {"selector": {"path": [True]}},
        {"selector": {"path": "$.rows[*]"}},
        {"source": "prefix${params.rows}"},
        {"source": "${chosen}"},
        {"selector": {"path": [], "where": []}},
        {"selector": {"path": [], "where": [{"path": [], "operator": "eval", "equals": 1}]}},
        {"value_type": "decimal"},
        {"cardinality": "first"},
        {"params": {}},
    ],
)
def test_extract_rejects_unsupported_selector_or_scope(scenario, change):
    root, case, _, save = scenario
    case["params"] = {"rows": []}
    case["steps"] = [extraction(**change)]
    save()
    with pytest.raises(InvalidAssetError):
        describe_case(case["id"], root)


async def test_extract_many_feeds_ordered_map_with_isolated_nested_extracts(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["params"] = {
        "rows": [{"code": "a", "value": 0}, {"code": "a", "value": False}],
        "code": "a",
    }
    case["steps"] = [
        extraction(cardinality="many"),
        {
            "step_id": "each",
            "type": "map",
            "source": "${chosen}",
            "flow_id": flow["id"],
            "params": {"value": "${item}"},
            "output": "all_values",
        },
    ]
    flow["steps"].insert(
        0,
        extraction(
            "${inputs.value}", selector={"path": ["value"]}, value_type="number", nullable=False
        ),
    )
    # Each invocation chooses its own typed value; boolean is allowed explicitly via object wrapper.
    flow["steps"][0]["selector"] = {"path": []}
    flow["steps"][0]["value_type"] = "object"
    flow["steps"][1]["params"] = {"value": "${chosen}"}
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert [r.body["value"]["value"] for r in state.requests] == [0, False]
    iterations = result["steps"][1]["trace"]["children"]
    assert iterations[0]["trace"]["children"][0]["trace"]["outputs"]["value"] == 0
    assert iterations[1]["trace"]["children"][0]["trace"]["outputs"]["value"] is False


async def test_extract_many_empty_is_success(scenario, server):
    root, case, _, save = scenario
    _, base = server
    case["params"] = {"rows": [], "code": "a"}
    case["steps"] = [extraction(cardinality="many")]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert result["steps"][0]["trace"]["outputs"] == []


async def test_dynamic_map_prechecks_all_extractions_before_first_child_request(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": [{"value": 1}, {}]},
            "output": "fetch",
        },
        {
            "step_id": "each",
            "type": "map",
            "source": "${fetch.body.echo}",
            "flow_id": flow["id"],
            "params": {"value": "${item}"},
        },
    ]
    flow["steps"].insert(
        0, extraction("${inputs.value}", selector={"path": ["value"]}, value_type="integer")
    )
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["steps"][1]["status"] == "failed"
    assert result["steps"][1]["error"]["kind"] == "variable_resolution"
    assert len(state.requests) == 1


async def test_extract_matches_null_but_not_missing_and_multiple_conditions(scenario, server):
    root, case, _, save = scenario
    _, base = server
    case["params"] = {"rows": [{}, {"code": None, "ok": False}, {"code": None, "ok": True}]}
    case["steps"] = [
        extraction(
            selector={
                "path": [],
                "where": [
                    {"path": ["code"], "equals": None},
                    {"path": ["ok"], "equals": False},
                ],
            }
        )
    ]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert result["steps"][0]["trace"]["outputs"] == {"code": None, "ok": False}


def decimal_step(operands, **overrides):
    return {
        "step_id": "amount",
        "type": "decimal",
        "operation": "add",
        "operands": operands,
        "scale": 2,
        "rounding": "HALF_UP",
        "output": "amount",
        **overrides,
    }


async def test_decimal_map_line_amount_sum_and_request_string(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["params"] = {
        "items": [{"price": "0.335", "quantity": 3}, {"price": "2.675", "quantity": 1}]
    }
    flow["steps"] = [
        decimal_step(["${inputs.value.price}", "${inputs.value.quantity}"], operation="multiply")
    ]
    flow["outputs"] = {"amount": "${amount}"}
    case["steps"] = [
        {
            "step_id": "lines",
            "type": "map",
            "source": "${params.items}",
            "flow_id": flow["id"],
            "params": {"value": "${item}"},
            "output": "lines",
        },
        decimal_step("${lines}", operation="sum", path=["amount"]),
        decimal_step(["${amount}", "0.10"], step_id="with_shipping", output="total"),
        {
            "step_id": "consume",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": {"total": "${total}"}},
        },
    ]
    save()
    described = describe_case(case["id"], root)
    assert described["steps"][1]["operation"] == "sum"
    assert described["steps"][1]["path"] == ["amount"]
    assert state.requests == []
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    # Line results are rounded at each explicit operation, then summed.
    assert result["steps"][0]["trace"]["outputs"] == [{"amount": "1.01"}, {"amount": "2.68"}]
    assert result["steps"][1]["trace"]["outputs"] == "3.69"
    assert state.requests[0].body["value"] == {"total": "3.79"}
    assert len(state.requests) == 1
    trace = result["steps"][1]["trace"]
    assert trace["calculations"][0]["status"] == "success"
    assert trace["bindings"][0]["source_node_id"] == result["steps"][0]["trace"]["node_id"]
    json.dumps(result)


@pytest.mark.parametrize("operands", [["NaN", 1], [True, 1], [None, 1], ["${params.missing}", 1]])
async def test_decimal_invalid_known_values_zero_requests(scenario, server, operands):
    root, case, _, save = scenario
    state, base = server
    case["steps"].insert(0, decimal_step(operands))
    save()
    with pytest.raises(InvalidInputError):
        await SingleApiEngine().execute(request(root, base))
    assert state.requests == []


async def test_decimal_dynamic_failure_retains_trace_and_stops(scenario, server):
    root, case, _, save = scenario
    state, base = server
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": {"price": "invalid-amount"}},
            "output": "fetch",
        },
        decimal_step(["${fetch.body.echo.price}", 2], operation="multiply"),
        {
            "step_id": "consume",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": "${amount}"},
        },
    ]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert [s["status"] for s in result["steps"]] == ["success", "failed", "skipped"]
    assert len(state.requests) == 1
    failed = result["steps"][1]
    assert failed["error"]["kind"] == "variable_resolution"
    assert "invalid-amount" not in failed["error"]["message"]
    assert failed["trace"]["outputs"] == {}
    assert failed["trace"]["calculations"][0]["status"] == "failed"
    assert (
        failed["trace"]["bindings"][0]["source_node_id"] == result["steps"][0]["trace"]["node_id"]
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation": "eval"},
        {"scale": True},
        {"scale": -1},
        {"scale": 9},
        {"rounding": "ambient"},
        {"rounding": None},
        {"operands": "prefix${params.x}"},
        {"operands": ["${future}"]},
        {"operands": ["${inputs.value}"]},
        {"path": ["amount"]},
        {"operation": "sum", "path": [-1]},
        {"output": "params"},
        {"identity": "buyer"},
        {"source": "${params.x}"},
    ],
)
def test_decimal_invalid_contract_rejected_in_description(scenario, overrides):
    root, case, _, save = scenario
    case["steps"] = [{**decimal_step([1, 2]), **overrides}]
    save()
    with pytest.raises(InvalidAssetError):
        describe_case(case["id"], root)


async def test_decimal_dynamic_map_prechecks_all_amounts_before_iteration_request(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": [{"price": "1.00"}, {"price": "NaN"}]},
            "output": "fetch",
        },
        {
            "step_id": "each",
            "type": "map",
            "source": "${fetch.body.echo}",
            "flow_id": flow["id"],
            "params": {"value": "${item}"},
        },
    ]
    flow["steps"].insert(0, decimal_step(["${inputs.value.price}", 2], operation="multiply"))
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["steps"][1]["status"] == "failed"
    assert len(state.requests) == 1


def assert_step(operation, actual, expected=None, **extra):
    value = {
        "step_id": "check_" + operation,
        "type": "assert",
        "operation": operation,
        "actual": actual,
    }
    if expected is not None:
        value["expected"] = expected
    value.update(extra)
    return value


@pytest.mark.parametrize(
    "actual,expected",
    [
        ({"count": True}, {"count": 1}),
        ([False], [0]),
        ([1], [1.0]),
        ({"nested": [{"value": False}]}, {"nested": [{"value": 0}]}),
    ],
)
def test_equal_distinguishes_nested_json_types(actual, expected):
    assert evaluate("equal", actual, expected) is False


async def test_assert_known_missing_parameter_fails_before_api_request(scenario, server):
    root, case, _, save = scenario
    state, base = server
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": {"ok": True}},
        },
        assert_step("non_empty", "${params.nonexistent}"),
    ]
    save()

    assert describe_case(case["id"], root)["steps"]
    with pytest.raises(InvalidInputError):
        await SingleApiEngine().execute(request(root, base))
    assert state.requests == []


def test_assert_flow_input_must_be_declared(scenario):
    root, case, flow, save = scenario
    flow["steps"] = [assert_step("non_empty", "${inputs.missing}")]
    flow["outputs"] = {"value": "${inputs.value}"}
    case["steps"] = [
        {
            "step_id": "call_flow",
            "type": "flow",
            "flow_id": flow["id"],
            "params": {"value": {"present": True}},
        }
    ]
    save()

    with pytest.raises(InvalidAssetError, match="未声明"):
        describe_case(case["id"], root)


async def test_assertions_are_traceable_and_stop_after_failure(scenario, server):
    root, case, _, save = scenario
    state, base = server
    case["params"] = {"order_no": "O-1", "quantity": 2, "amount": "3.70"}
    case["steps"] = [
        assert_step("non_empty", "${params.order_no}"),
        assert_step("positive_integer", "${params.quantity}"),
        assert_step("decimal_equal", "${params.amount}", "3.70"),
        assert_step("equal", "${params.quantity}", 3),
        {
            "step_id": "never",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": 1},
        },
    ]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert [step["status"] for step in result["steps"]] == [
        "success",
        "success",
        "success",
        "failed",
        "skipped",
    ]
    assert result["steps"][3]["trace"]["assertions"][0]["operation"] == "equal"
    assert result["steps"][3]["trace"]["assertions"][0]["actual"] == 2
    assert state.requests == []


@pytest.mark.parametrize(
    "operation,actual,expected",
    [
        ("missing", "${params.x}", None),
        ("equal", "${params.x}", None),
        ("decimal_equal", "${future}", 1),
        ("positive_integer", "literal", None),
    ],
)
def test_assertion_contract_rejected(scenario, operation, actual, expected):
    root, case, _, save = scenario
    case["steps"] = [assert_step(operation, actual, expected)]
    save()
    with pytest.raises(InvalidAssetError):
        describe_case(case["id"], root)


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation": "eval"},
        {"source": "prefix${params.rows}"},
        {"source": "${future}"},
        {"path": [-1]},
        {"path": [True]},
        {"path": "${params.path}"},
        {"output": "params"},
        {"identity": "buyer"},
    ],
)
def test_transform_contract_rejected(scenario, overrides):
    root, case, _, save = scenario
    case["steps"] = [
        {
            "step_id": "list",
            "type": "transform",
            "operation": "project",
            "source": "${params.rows}",
            "path": [],
            "output": "rows",
            **overrides,
        }
    ]
    save()
    with pytest.raises(InvalidAssetError):
        describe_case(case["id"], root)


def dynamic_map_case(case, flow, *, dynamic_source=False):
    case["params"] = {"rows": [{"present": 1}, {"present": 2}]}
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": {"shared": True}},
            "output": "fetch",
        },
        {
            "step_id": "each",
            "type": "map",
            "flow_id": flow["id"],
            "source": "${params.rows}",
            "params": {"value": {"item": "${item.present}", "shared": "${fetch.body.echo.shared}"}},
            "output": "mapped",
        },
    ]
    if dynamic_source:
        case["steps"][0]["params"] = {"value": case["params"]["rows"]}
        case["steps"][1]["source"] = "${fetch.body.echo}"
        case["steps"][1]["params"]["value"]["shared"] = "${params.shared}"
        case["params"]["shared"] = True


@pytest.mark.parametrize("dynamic_source", [False, True])
async def test_preflight_dynamic_map_binding_runs(scenario, server, dynamic_source):
    root, case, flow, save = scenario
    state, base = server
    dynamic_map_case(case, flow, dynamic_source=dynamic_source)
    save()
    assert describe_case(case["id"], root)
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert [r.body["value"] for r in state.requests[1:]] == [
        {"item": 1, "shared": True},
        {"item": 2, "shared": True},
    ]


async def test_preflight_dynamic_map_keeps_known_item_validation(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    dynamic_map_case(case, flow)
    case["params"]["rows"][1] = {}
    save()
    with pytest.raises(InvalidInputError):
        await SingleApiEngine().execute(request(root, base))
    assert state.requests == []


@pytest.mark.parametrize("mixed", [False, True])
async def test_preflight_flow_output_known_missing_is_zero_request(scenario, server, mixed):
    root, case, flow, save = scenario
    state, base = server
    flow["outputs"] = {"value": "${inputs.value.missing}"}
    case["steps"] = case["steps"][:1]
    if mixed:
        case["steps"].insert(
            0,
            {
                "step_id": "fetch",
                "type": "api",
                "api_id": "api.demo.echo_object",
                "params": {"value": 1},
                "output": "fetch",
            },
        )
        case["steps"][1]["params"]["value"]["dynamic"] = "${fetch.body.echo}"
    save()
    with pytest.raises(InvalidInputError):
        await SingleApiEngine().execute(request(root, base))
    assert state.requests == []


async def test_preflight_flow_output_known_valid_and_dynamic_are_preserved(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["steps"] = case["steps"][:1]
    flow["outputs"] = {"known": "${inputs.value.own}", "dynamic": "${echo.body.echo.shared}"}
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert result["steps"][0]["trace"]["outputs"] == {"known": 1, "dynamic": True}
    assert len(state.requests) == 1


@pytest.mark.parametrize(
    "expected",
    [
        {"nested": "${inputs.undeclared}"},
        ["${future.body}"],
        {"nested": ["${inputs.undeclared}"]},
    ],
)
def test_nested_expected_rejects_invalid_reference(scenario, expected):
    root, case, flow, save = scenario
    flow["outputs"] = {"value": "${inputs.value}"}
    flow["steps"] = [assert_step("equal", "${inputs.value}", expected)]
    save()
    with pytest.raises(InvalidAssetError):
        describe_case(case["id"], root)


async def test_nested_expected_valid_dynamic_reference_runs(scenario, server):
    root, case, flow, save = scenario
    state, base = server
    case["params"] = {"x": {"nested": [True]}}
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": True},
            "output": "fetch",
        },
        assert_step("equal", "${params.x}", {"nested": ["${fetch.body.echo}"]}),
    ]
    save()
    result = (await SingleApiEngine().execute(request(root, base))).to_dict()
    assert result["status"] == "success"
    assert len(state.requests) == 1


@pytest.mark.parametrize("missing", [True, False])
async def test_preflight_nested_flow_preserves_partial_object(scenario, server, missing):
    root, case, flow, save = scenario
    state, base = server
    flow["outputs"] = {
        "value": "${inputs.value.missing}" if missing else "${inputs.value}",
    }
    outer = {
        "schema_version": "0.3",
        "id": "flow.demo.outer",
        "inputs": flow["inputs"],
        "outputs": {"value": "${inner.value}"},
        "steps": [
            {
                "step_id": "inner",
                "type": "flow",
                "flow_id": flow["id"],
                "params": {"value": "${inputs.value}"},
                "output": "inner",
            }
        ],
    }
    case["steps"] = [
        {
            "step_id": "fetch",
            "type": "api",
            "api_id": "api.demo.echo_object",
            "params": {"value": True},
            "output": "fetch",
        },
        {
            "step_id": "outer",
            "type": "flow",
            "flow_id": outer["id"],
            "params": {"value": {"known": 1, "dynamic": "${fetch.body.echo}"}},
            "output": "outer",
        },
    ]
    save()
    (root / "scenarios/demo/outer.flow.yaml").write_text(yaml.safe_dump(outer))
    if missing:
        with pytest.raises(InvalidInputError):
            await SingleApiEngine().execute(request(root, base))
        assert state.requests == []
    else:
        result = (await SingleApiEngine().execute(request(root, base))).to_dict()
        assert result["status"] == "success"
        assert result["steps"][1]["trace"]["outputs"] == {"value": {"known": 1, "dynamic": True}}
        assert len(state.requests) == 2
