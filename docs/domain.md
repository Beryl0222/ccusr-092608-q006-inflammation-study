# 领域约定

记录大型队列衍生分析、样本谱系、科学审阅和公开结论事件。

## 聚合对象

- `cohort_snapshot`：队列快照。冻结后只读，撤回或质控更正不修改快照，只产生失效事件。
- `data_asset`：版本化数据资产，包括变量字典（`variable_dictionary`）、样本排除规则集（`exclusion_rule_set`）、标志物批次（`marker_batch`）、影像质控（`imaging_qc`）、遗传工具变量（`genetic_instrument`）、协变量方案（`covariate_scheme`）、队列名册（`cohort_roster`，承载快照样本集）。
- `participant`：参与者。登记撤回与逐次样本排除理由。
- `access_grant`：访问授权。由数据管理员签发与撤回。
- `analysis_plan`：分析计划。统计人员登记后锁定，已签发结论不得由统计人员改写。
- `quota_pool`：有限分析名额池，并发预留不得超额。
- `run_record`：一次运行及其回执、输入指纹与参数指纹。
- `research_claim`：研究结论（风险表述）。区分 `association`（相关性）、`causal`（因果推断）、`mechanism_provisional`（待验证机制）。
- `public_material`：公开材料（论文、新闻稿、回复信等），必须引用冻结的分析版本。

## 事件

事件类型包括 `COHORT_FROZEN`、`DATA_ASSET_VERSIONED`、`ACCESS_GRANTED`、`ACCESS_REVOKED`、`WITHDRAWAL_RECEIVED`、`PARTICIPANT_EXCLUDED`、`QC_RULE_CORRECTED`、`PLAN_REGISTERED`、`QUOTA_POOL_OPENED`、`SLOT_RESERVED`、`SLOT_RELEASED`、`RUN_RECORDED`、`RUN_QUARANTINED`、`CLAIM_REVIEWED`、`CLAIM_ISSUED`、`MATERIAL_PUBLISHED`、`DEPENDENCY_INVALIDATED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `COHORT_FROZEN`：载荷需包含 `definition_hash`。
- `DATA_ASSET_VERSIONED`：载荷需包含 `asset_kind`、`asset_version`、`fingerprint`。
- `ACCESS_GRANTED` / `ACCESS_REVOKED`：载荷需包含 `subject_id`、`asset_id`。
- `WITHDRAWAL_RECEIVED`：载荷需包含 `participant_id`。
- `PARTICIPANT_EXCLUDED`：载荷需包含 `participant_id`、`reason_code`、`rule_version`。
- `QC_RULE_CORRECTED`：载荷需包含 `asset_id`、`old_version`、`new_version`。
- `PLAN_REGISTERED`：载荷还需包含 `variable_versions`、`exclusion_rules`、`cohort_id`、`code_version`。
- `QUOTA_POOL_OPENED`：载荷需包含 `capacity`。
- `SLOT_RESERVED` / `SLOT_RELEASED`：载荷需包含 `quota_pool_id`、`holder_id`、`slot_count`。
- `RUN_RECORDED`：载荷还需包含 `input_hash`、`parameter_hash`、`run_receipt`、`plan_id`。
- `RUN_QUARANTINED`：载荷需包含 `run_receipt`、`reason_code`。
- `CLAIM_REVIEWED`：载荷还需包含 `claim_kind`、`reviewer_id`。
- `CLAIM_ISSUED`：载荷需包含 `claim_kind`、`run_id`、`statement_hash`、`frozen_references`。
- `MATERIAL_PUBLISHED`：载荷需包含 `material_kind`、`frozen_references`。
- `DEPENDENCY_INVALIDATED`：载荷需包含 `reason_code`、`source_event_id`。

`claim_kind` 取值为 `association`、`causal`、`mechanism_provisional`；`asset_kind` 取值见上节。

相同事件标识的业务幂等、冲突隔离、名额预留、依赖级联失效、签发锁定与最小谱系投影由上层服务负责；本仓库只定义可稳定交换的基础事实。
