"""迁移模板与调用身份的真实执行路径；所有请求仅进内存 HTTP transport。"""

import json
from datetime import datetime, timedelta

import httpx
import pytest
import yaml

from testplat_engine import (
    ApiExecutionRequest,
    AuthTable,
    CaseExecutionRequest,
    InvalidEnvironmentError,
    InvalidInputError,
    SecretStr,
    SingleApiEngine,
    StaticHeaderAuth,
)
from testplat_engine.plugins.httpx_client import HttpxClient


def write_api(root, *, projects=None):
    path = root / "api/demo/create.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "id": "api.demo.create",
                "name": "结构化创建",
                "host": "service",
                "method": "POST",
                "url": "/create",
                "auth": "buyer",
                "projects": projects or [],
                "request": {
                    "template": "structured",
                    "type": "json",
                    "inputs": {"channel": {"default": "PC"}},
                    "body": {
                        "id": "${product.id!}",
                        "quantity": "${quantity!}",
                        "items": "${items!}",
                        "enabled": "${enabled}",
                        "optional": "${product.unknown}",
                        "nullable": "${nullable}",
                        "channel": "${channel|OTHER}",
                        "source": "${source|TAOBAO}",
                        "remark": "测试-${now()}",
                        "date": "${date_add_days(shipping.days)}",
                    },
                    "headers": {"X-Quantity": "${quantity!}"},
                },
                "success": {"path": "$.errorCode", "value": "0"},
            },
            allow_unicode=True,
        )
    )


def table():
    return AuthTable(
        identities={
            name: StaticHeaderAuth(headers={"X-TOKEN": SecretStr("fake-" + name)})
            for name in ("buyer", "seller")
        }
    )


def params():
    return {
        "product": {"id": "p1"},
        "quantity": 0,
        "items": [{"id": 1}],
        "enabled": False,
        "nullable": None,
        "shipping": {"days": 2},
    }


async def test_structured_wire_types_defaults_dates_and_caller_identity(tmp_path):
    write_api(tmp_path)
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"errorCode": "0"})

    async with HttpxClient(transport=httpx.MockTransport(handle)) as client:
        engine = SingleApiEngine(client)
        for identity in ("seller", "buyer", "none"):
            outcome = await engine.execute(
                ApiExecutionRequest(
                    api_id="api.demo.create",
                    assets_root=tmp_path,
                    environment={"api_hosts": {"service": "http://fake"}},
                    params=params(),
                    auth=table(),
                    identity=identity,
                )
            )
            assert outcome.status == "success"
            assert "fake-seller" not in json.dumps(outcome.to_dict())
    body = json.loads(sent[0].content)
    assert body["quantity"] == 0 and body["enabled"] is False
    assert body["items"] == [{"id": 1}] and body["nullable"] is None
    assert "optional" not in body
    assert body["channel"] == "PC" and body["source"] == "TAOBAO"
    assert body["date"] == (datetime.now().astimezone().date() + timedelta(days=2)).isoformat()
    assert body["remark"].startswith("测试-") and "${" not in body["remark"]
    assert sent[0].headers["X-Quantity"] == "0"
    assert [r.headers.get("X-TOKEN") for r in sent] == ["fake-seller", "fake-buyer", None]


@pytest.mark.parametrize("product", [{}, {"id": None}])
async def test_missing_required_leaf_never_sends_http(tmp_path, product):
    write_api(tmp_path)
    sent = []
    async with HttpxClient(transport=httpx.MockTransport(lambda r: sent.append(r))) as client:
        with pytest.raises(InvalidInputError, match="product.id"):
            await SingleApiEngine(client).execute(
                ApiExecutionRequest(
                    api_id="api.demo.create",
                    assets_root=tmp_path,
                    environment={"api_hosts": {"service": "http://fake"}},
                    params={**params(), "product": product},
                    auth=table(),
                )
            )
    assert sent == []


async def test_override_bypasses_body_but_not_header_parameters(tmp_path):
    write_api(tmp_path)
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"errorCode": "0"})

    async with HttpxClient(transport=httpx.MockTransport(handle)) as client:
        result = await SingleApiEngine(client).execute(
            ApiExecutionRequest(
                api_id="api.demo.create",
                assets_root=tmp_path,
                environment={"api_hosts": {"service": "http://fake"}},
                params={"quantity": 1},
                identity="none",
                request_override={"body": {"literal": "${quantity!}"}},
            )
        )
    assert result.status == "success" and sent == [{"literal": "${quantity!}"}]


@pytest.mark.parametrize("project", [None, "e3e3"])
async def test_project_contract_refuses_wrong_or_missing_context(tmp_path, project):
    write_api(tmp_path, projects=["go2"])
    with pytest.raises(InvalidEnvironmentError, match="业务项目"):
        await SingleApiEngine().execute(
            ApiExecutionRequest(
                api_id="api.demo.create",
                assets_root=tmp_path,
                params=params(),
                environment={"api_hosts": {"service": "http://fake"}, "business_project": project},
                identity="none",
            )
        )


async def test_case_steps_control_identities_and_preflight_missing_identity(tmp_path):
    write_api(tmp_path)
    path = tmp_path / "scenarios/demo/call.case.yaml"
    path.parent.mkdir(parents=True)
    document = {
        "schema_version": "0.2",
        "id": "case.demo.call",
        "steps": [
            {
                "step_id": name,
                "type": "api",
                "api_id": "api.demo.create",
                "identity": name,
                "params": params(),
            }
            for name in ("buyer", "seller", "none")
        ],
    }
    path.write_text(yaml.safe_dump(document))
    sent = []

    def handle(request):
        sent.append(request.headers.get("X-TOKEN"))
        return httpx.Response(200, json={"errorCode": "0"})

    async with HttpxClient(transport=httpx.MockTransport(handle)) as client:
        request = CaseExecutionRequest(
            case_id="case.demo.call",
            assets_root=tmp_path,
            environment={"api_hosts": {"service": "http://fake"}},
            params={},
            auth=table(),
        )
        result = await SingleApiEngine(client).execute(request)
        assert result.status == "success"
        assert sent == ["fake-buyer", "fake-seller", None]
        document["steps"][1]["identity"] = "missing"
        path.write_text(yaml.safe_dump(document))
        sent.clear()
        with pytest.raises(InvalidEnvironmentError, match="missing"):
            await SingleApiEngine(client).execute(request)
        assert sent == []
