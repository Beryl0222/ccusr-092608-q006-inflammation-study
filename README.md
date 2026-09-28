# 炎症心脏研究衍生分析库

面向近五十万人炎症与心脏研究的衍生分析登记服务：管理队列快照、变量字典、
样本排除理由、标志物批次、影像质控、遗传工具变量、协变量方案、分析计划、
运行摘要、结论边界与公开材料；在参与者撤回或质控规则更正时沿样本和产物
依赖标记需要重算的结果，不删除历史发表快照。

## 治理规则

- **角色边界**：统计人员登记计划/运行/签发/公开，不能自行改写已签发结果；
  数据管理员只处理队列、资产访问与撤回；科学审阅者区分相关性、因果推断与
  待验证机制；平台主管维护名额并可见全量谱系。
- **运行回执**：相同回执 + 相同输入/参数指纹沿用原记录；指纹不一致则隔离，
  原记录不动。
- **有限名额**：按计划设池，终态提交消耗名额，并发分配不超额。
- **签发冻结**：结果签发后不可改写；任何公开表述必须引用冻结的分析版本。
- **失效传播**：撤回/质控更正沿 运行 → 结论 → 公开材料 链标记重算，
  历史事件只追加、不删除。
- **最小谱系**：按角色裁剪字段；命令行可从一条风险表述追溯到队列、
  代码版本、参数、审阅意见和受影响的后续材料。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `data/demo.jsonl`：端到端演示事件流（由 demo 子命令生成）。
- `src/inflammation_study/`：契约校验、只追加事件存储、登记服务与命令行。
- `tests/`：信封/存储测试与服务业务规则测试。
- `docs/domain.md`：领域对象、事件与业务规则语义。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 命令行

单事件契约校验（样例有效输出 `valid`）：

```bash
PYTHONPATH=src python3 -m inflammation_study.cli validate contracts/domain.schema.json data/sample.json
```

生成端到端演示事件流（存储须为空）：

```bash
PYTHONPATH=src python3 -m inflammation_study.cli demo data/demo.jsonl
```

从一条风险表述文本找到结论：

```bash
PYTHONPATH=src python3 -m inflammation_study.cli find data/demo.jsonl "CRP 升高"
# claim-crp-lvef
```

从公开材料（或结论、运行）追溯最小数据谱系，按角色裁剪：

```bash
PYTHONPATH=src python3 -m inflammation_study.cli trace data/demo.jsonl mat-faq-crp --role admin
PYTHONPATH=src python3 -m inflammation_study.cli trace data/demo.jsonl mat-faq-crp --role public
PYTHONPATH=src python3 -m inflammation_study.cli trace data/demo.jsonl run-2026-0001 --role steward
```

主管视图包含队列、计划变量版本、运行输入/参数指纹、代码版本、签发版本、
审阅意见与 `event_log`；公开视图只保留冻结版本编号、失效状态与资产是否
过期，看不到样本编号、指纹和审阅人。

## 作为库使用

```python
from inflammation_study.registry import Registry, Principal, STATISTICIAN
from inflammation_study.store import EventStore

registry = Registry(EventStore("events.jsonl"))
stat = Principal("statistician-chen", STATISTICIAN)
```

事件存储为 JSONL 只追加文件，进程内加锁、落盘用 `fcntl` 互斥；
传入 `EventStore()`（无路径）得到纯内存存储，适合测试。
