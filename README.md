# 炎症心脏研究衍生分析库

为近五十万人队列的衍生分析提供**登记服务**：管理队列快照、变量字典、样本排除理由、标志物批次、影像质控、遗传工具变量、协变量方案、分析计划、运行摘要、结论边界与公开材料。论文修订时可从任意一条风险表述追溯其依据，并在参与者撤回或质控规则更正时准确找出需要重算的结论。

## 设计原则

- **只追加事件日志**：队列冻结、计划登记、运行记录、审阅签发、撤回与失效全部是不可变事件；任何更正都产生新事件，历史发表快照永不删除。
- **角色分离**：平台主管冻结队列/名额；统计人员登记计划与运行，计划登记后锁定、已签发结论不可自行改写；数据管理员只处理访问授权、撤回与样本排除；科学审阅者把结论区分为 `association`（相关性）、`causal`（因果推断）、`mechanism_provisional`（待验证机制）。
- **运行回执幂等**：相同回执且 `input_hash`/`parameter_hash` 一致沿用原记录；指纹不一致则发 `RUN_QUARANTINED` 隔离，原记录不动。
- **失效级联**：撤回、排除或质控更正沿 `队列 → 计划 → 运行 → 结论 → 公开材料` 标记 `DEPENDENCY_INVALIDATED`，论文修订时据此重算。
- **公开表述必须引用冻结版本**：公开材料只能引用已 `CLAIM_ISSUED` 的结论或有效运行，签发时自动冻结队列、代码版本与运行指纹。
- **名额并发安全**：`quota_pool` 预留串行校验，并发请求绝不超出容量。
- **最小数据谱系**：按角色与数据访问授权投影；无授权资产只显示标识与 `access_required`，统计人员视角不返回参与者标识。

## 目录

- `contracts/domain.schema.json`：聚合对象、事件类型与载荷字段契约。
- `docs/domain.md`：领域对象与事件语义。
- `data/sample.json`：可直接校验的事件信封样例。
- `data/demo_events.jsonl`：用于 `trace` 命令的演示登记库。
- `src/inflammation_study/`
  - `contracts.py`：基础事件信封/载荷/枚举校验（不改写调用方输入）。
  - `events.py`：追加写事件存储（内存 + JSONL），event_id 幂等与冲突隔离，聚合版本单调递增。
  - `registry.py`：登记服务，承载授权、锁定、回执幂等、名额与失效级联。
  - `lineage.py`：按权限投影的最小谱系视图。
  - `cli.py`：契约校验与 `trace` 追溯命令。
- `tests/`：契约、服务、并发名额、级联与谱系测试。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 事件契约校验

```bash
PYTHONPATH=src python3 -m inflammation_study.cli contracts/domain.schema.json data/sample.json
# 等价: python -m inflammation_study.cli validate <schema.json> <event.json>
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 从风险表述追溯谱系

```bash
PYTHONPATH=src python3 -m inflammation_study.cli trace \
  --store data/demo_events.jsonl \
  --claim claim-high-inflamation-hf \
  --as rev1:reviewer        # 或 stat1:statistician（参与者字段将被掩码）
```

输出 JSON 包含：结论类型与审阅意见、冻结引用（队列/代码版本/运行输入与参数指纹）、计划引用的变量字典与排除规则版本、队列撤回/排除来源，以及受影响、需要重算的后续公开材料。角色取值：`admin`、`statistician`、`steward`、`reviewer`。

## 服务 API 速览

```python
reg.freeze_cohort(admin, "cohort-1", definition_hash, participant_ids)
reg.version_asset(steward, "qc-cmr-r7", "imaging_qc", fingerprint)
reg.grant_access(steward, "stat1", "cohort-1")
reg.register_plan(stat, "plan-A17", "cohort-1", "git:f2c9d1a",
                  variable_versions={"dict-inflam-markers": "3"},
                  exclusion_rules=["excl-core", "qc-cmr-r7"],
                  covariate_scheme_id="cov-main")
reg.open_quota_pool(admin, "slots-2026Q4", 20)
reg.reserve_slot(stat, "slots-2026Q4", "stat1")
reg.record_run(stat, "run-A17-04", "plan-A17", "receipt-8f31a9", input_hash, parameter_hash)
reg.review_claim(reviewer, "claim-...", "run-A17-04", "association", "approved", "审阅意见")
reg.issue_claim(reviewer, "claim-...", statement_hash)
reg.publish_material(admin, "mat-...", "paper", [{"kind": "claim", "id": "claim-..."}])
reg.receive_withdrawal(steward, "P100002")      # 自动级联标记重算
reg.correct_qc_rule(steward, "qc-cmr-r7", new_fingerprint)
```

存储可传 `EventStore(path="events.jsonl", schema=schema)` 落盘，重放同一文件即可重建全部状态。
