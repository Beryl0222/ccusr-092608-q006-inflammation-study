# 领域约定

记录大型队列衍生分析、样本谱系、科学审阅和公开结论事件。

聚合对象包括`cohort_snapshot`、`analysis_plan`、`run_record`、`research_claim`。事件类型包括`COHORT_FROZEN`、`PLAN_REGISTERED`、`RUN_RECORDED`、`CLAIM_REVIEWED`、`DEPENDENCY_INVALIDATED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `PLAN_REGISTERED`：载荷还需包含 `variable_versions`, `exclusion_rules`。
- `RUN_RECORDED`：载荷还需包含 `input_hash`, `parameter_hash`。
- `CLAIM_REVIEWED`：载荷还需包含 `claim_kind`, `reviewer_id`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
