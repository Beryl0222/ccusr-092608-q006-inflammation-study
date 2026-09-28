# 领域约定

记录大型队列衍生分析、样本谱系、科学审阅和公开结论事件。系统只追加事件、
从不删除历史快照；撤回与质控更正通过沿依赖链追加失效事件表达。

## 角色

| 角色 | 职责边界 |
| --- | --- |
| `statistician` 统计人员 | 登记分析计划、申请名额、提交运行、签发结果、发布公开材料；不能审阅自己的结论 |
| `data_steward` 数据管理员 | 冻结队列快照、登记/更正数据资产、登记参与者撤回；只处理访问与撤回 |
| `scientific_reviewer` 科学审阅者 | 出具审阅结论，区分相关性、因果推断、待验证机制，接受或退回 |
| `platform_admin` 平台主管 | 创建名额池、查看全量谱系（最小数据谱系中的完整视图） |
| `public` 匿名视角 | 只能查看已公开材料的谱系，且只看到冻结版本编号等基线字段 |

## 聚合对象

- `cohort_snapshot`：队列快照，冻结后不可改写。
- `data_asset`：变量字典 `variable_dictionary`、样本排除理由 `exclusion_reason`、
  标志物批次 `biomarker_batch`、影像质控 `imaging_qc`、遗传工具变量
  `genetic_instrument`、协变量方案 `covariate_scheme`；更正以新指纹的新版本追加。
- `analysis_plan`：分析计划，携带变量版本、排除规则、引用资产与名额池。
- `run_record`：运行记录或隔离记录；结果一经 `RESULT_ISSUED` 即冻结。
- `research_claim`：科学结论，只能引用运行当前冻结的结果版本。
- `public_material`：公开材料，必须引用已接受、未失效结论所冻结的分析版本。
- `participant_withdrawal`：参与者撤回，同一参与者在同一队列重复撤回按幂等处理。

## 事件类型

| 事件 | 聚合 | 语义 |
| --- | --- | --- |
| `COHORT_FROZEN` | cohort_snapshot | 队列快照冻结 |
| `ASSET_REGISTERED` | data_asset | 资产登记或指纹版本更正 |
| `PLAN_REGISTERED` | analysis_plan | 计划登记 |
| `SLOT_GRANTED` | analysis_plan | 创建名额池（`pool_created`）或授予名额 |
| `SLOT_RELEASED` | analysis_plan | 名额被一次终态提交消耗 |
| `RUN_RECORDED` | run_record | 正式运行记录 |
| `RUN_QUARANTINED` | run_record | 同回执指纹冲突，原记录保留、提交隔离 |
| `RESULT_ISSUED` | run_record | 结果签发并冻结，不可改写 |
| `CLAIM_REVIEWED` | research_claim | 科学审阅（accepted/returned） |
| `DEPENDENCY_INVALIDATED` | run/research_claim/public_material | 标记需要重算，不删除历史 |
| `MATERIAL_PUBLISHED` | public_material | 公开材料发布 |
| `WITHDRAWAL_RECEIVED` | participant_withdrawal | 参与者撤回登记 |

所有发生时间都必须携带时区，版本号从 1 开始按聚合严格递增，
基础校验不会改写调用方输入。

## 关键业务规则

### 运行回执幂等与冲突隔离

- 相同 `receipt_id` 且 `input_hash`、`parameter_hash` 一致：沿用原运行记录，
  不产生新事件、不消耗名额。
- 相同 `receipt_id` 但任一指纹不一致：原记录保留不动，本次提交写入
  `RUN_QUARANTINED` 隔离记录（载荷同时给出提交指纹与已存指纹），并消耗一个名额。
- 完全相同的冲突提交再次出现，沿用原隔离记录。

### 分析名额

- 名额按计划设池，容量由平台主管设定；授予数减去释放数不得超过容量。
- 一次正式运行或一次隔离提交都构成终态提交，消耗（释放）一个名额。
- 名额授予在进程锁内记账，并发申请不会超额；超额返回 `quota_exhausted`。

### 签发锁定与审阅分级

- 结果签发后，同一运行不能再补发其他结果版本（`state_error`）。
- `claim_kind` 分三档：`correlation_association`（相关性）、
  `causal_inference`（因果推断）、`mechanism_provisional`（待验证机制）。
- 审阅必须引用运行当前冻结的 `frozen_result_version`；退回的结论不能公开引用。

### 失效传播（标记重算，不删历史）

参与者撤回或质控规则更正时，沿以下依赖链逐级追加
`DEPENDENCY_INVALIDATED`：

```
run_record ──> research_claim（该运行上的全部结论）──> public_material
```

- 撤回只影响样本编号命中的运行；结论与材料收到的是 `upstream_invalidation`。
- 质控更正只影响引用了旧资产指纹的运行。
- 已失效的冻结版本不能支撑新结论或新公开材料；需用新指纹重跑、
  签发新版本后再审阅、再公开。历史发布事件全部保留。

## 事件载荷必填项

见 `contracts/domain.schema.json` 的 `payload_required_by_event` 与
`payload_enums`，由基础契约校验器统一校验。

相同事件标识的业务幂等、冲突隔离和状态推进由上层登记服务负责；
契约仓库只定义可稳定交换的基础事实。
