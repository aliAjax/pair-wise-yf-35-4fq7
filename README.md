# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件。

## 案件状态机与B样复核

案件状态：`open → suspended → review_pending →（suspended | review_conflict）→ hearing → closed → appeal → closed`。

- 案件立案（`case.open`）后，初检阳性即执行 `provisional_suspend` 进入临时禁赛，并固定 `initial_result=positive`。
- 运动员方可在保留期内执行 `request_review` 申请B样复核（需 `requested_at`）。创建案件时必须提供 `filed_at`，系统据此计算 `retention_until`（默认立案日起14天，见 `src/rules.py` 的 `REVIEW_RETENTION_DAYS`），逾期申请返回400。
- 实验室（`lab` 角色）通过 `record_review` 回传复核结论（需 `review_result` 为 `positive`/`not_detected` 与 `lab_report_id`）：
  - 复核仍为阳性：回到 `suspended` 继续临时禁赛，随后安排听证、作出处罚。
  - 复核未检出：进入 `review_conflict`，初检与复核两份结论都保留在案件数据中，只能由案件负责人执行 `redispose`（仅允许 `decision=no_sanction`）重新处置后结束。
- 保留期已过且无复核结论时，负责人可执行 `lapse_retention`，案件直接以无处罚结束。
- `initial_result` 与 `review_result` 是受保护结论字段，只能由 `provisional_suspend` / `record_review` 写入；任何其他动作携带这些字段都会返回409，后到的普通更新不能覆盖已有结论。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
