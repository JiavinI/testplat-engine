# Case 契约 Golden / Mutation 清单（Engine 0.2）

与 `notes/phase3-case-contract-v0.2.md` G1–G6、M1–M8 对齐。

| ID | 资产 / 构造 | 本批契约期望 | 运行期 |
|---|---|---|---|
| G1 | `case.demo.g1_success` | 加载成功；2 步；output=created | pending Agent 2 |
| G2 | `case.demo.g2_api_assert_failed` | 加载成功 | pending：Case failed；首步 assertion；后步 skipped |
| G3 | `case.demo.g3_typed_propagation` | 加载成功；3 步 | pending：类型保持 |
| G4a | `case.demo.g4a_undeclared_alias` | preflight `invalid_asset` | 不发 HTTP |
| G4b | `case.demo.g4b_missing_runtime_path` | 加载成功 | pending：variable_resolution + skipped |
| G5 | `case.demo.g5_unsupported_step` | preflight `invalid_asset`（不支持 type） | 0 HTTP |
| G6a | `case.demo.g6a_timeout` | 加载成功 | pending：timeout + partial_outcome |
| G6b | `case.demo.g6b_transport` | 加载成功 | pending：transport + partial_outcome |
| M1 | 测试内构造重复 step_id | `invalid_asset` | — |
| M2 | 测试内构造重复 output | `invalid_asset` | — |
| M3 | 测试内构造前向引用 | `invalid_asset` | — |
| M4 | 测试内构造未知字段 | `invalid_asset` | — |
| M5 | `case.demo.m5_omit_output` | 合法 | — |
| M6 | 测试内构造 `${steps.0...}` | `invalid_asset` | — |
| M7 | 文件 id 与请求不一致 | `invalid_asset` | — |
| M8 | `case.demo.m8_missing_params` | 加载后 `validate` → `invalid_input` | 不创建记录 |
