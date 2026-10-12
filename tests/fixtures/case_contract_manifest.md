# Case 契约 Golden / Mutation 清单（Engine 0.2）

与 `notes/phase3-case-contract-v0.2.md` G1–G6、M1–M8 对齐。
加载契约在 `tests/adapters/test_case_loader.py`；运行期真实结果在
`tests/engine/test_case_golden.py`（公开 `execute(CaseExecutionRequest)` + 本地 HTTP server/可控 client）。

| ID | 资产 / 构造 | 本批契约期望 | 运行期实际结果（已由测试固定） |
|---|---|---|---|
| G1 | `case.demo.g1_success` | 加载成功；2 步；output=created | Case success；summary 2/2/0/0；get 请求头收到 `101`；output 仅回显 `created` |
| G2 | `case.demo.g2_api_assert_failed` | 加载成功 | Case failed（返回值，非异常）；首步 `assertion`；次步 skipped（无 HTTP）；summary 2/0/1/1 |
| G3 | `case.demo.g3_typed_propagation` | 加载成功；3 步 | 3 success；服务端收到 `42`(int)、`true`(bool)、对象 |
| G4a | `case.demo.g4a_undeclared_alias` | preflight `invalid_asset` | 同左；0 次 HTTP |
| G4b | `case.demo.g4b_missing_runtime_path` | 加载成功 | create success；consume failed `variable_resolution`（step_id/path，request=null）；after skipped；summary 3/1/1/1 |
| G5 | `case.demo.g5_unsupported_step` | preflight `invalid_asset`（不支持 type） | 同左，message 含 `ui`；0 次 HTTP |
| G6a | `case.demo.g6a_timeout` | 加载成功 | 抛 `timeout/execution`；partial = success/failed/skipped |
| G6b | `case.demo.g6b_transport` | 加载成功 | 抛 `transport/execution`；partial = success/failed/skipped |
| M1 | 测试内构造重复 step_id | `invalid_asset` | — |
| M2 | 测试内构造重复 output | `invalid_asset` | — |
| M3 | 测试内构造前向引用 | `invalid_asset` | — |
| M4 | 测试内构造未知字段 | `invalid_asset` | — |
| M5 | `case.demo.m5_omit_output` | 合法 | 运行 success（单步） |
| M6 | 测试内构造 `${steps.0...}` | `invalid_asset` | — |
| M7 | 文件 id 与请求不一致 | `invalid_asset` | — |
| M8 | `case.demo.m8_missing_params` | 加载后 `validate` → `invalid_input` | 公开 execute 抛 `invalid_input/preflight`；0 次 HTTP |

说明：`api.demo.get` 增加了 `X-Resource-Id: "${resource_id}"` 请求头，使"后一步确实收到前一步 alias 值"在请求侧可观察。
