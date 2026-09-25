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

## B样复核流程

初检阳性（adverse）立案后，案件进入临时禁赛，复核回路如下：

- 立案时把初检结论快照到`initial_result`，并写入复核保留期`review_deadline`（默认立案后30天，创建时可用ISO日期覆盖）。
- `request_review`（`open`/`suspended` → `review_pending`）：运动员方（`athlete`/`admin`/`panel`角色）在保留期内提出复核；保留期已过或已有复核结论会被拒绝。
- `report_review`（`review_pending` → `suspended`/`review_disputed`）：实验室回传`review_result`。`confirmed`表示仍为阳性，回到`suspended`继续临时禁赛；`negative`表示未检出，与初检不一致，转入`review_disputed`。
- `resolve_review`（`review_disputed` → `closed`）：案件负责人处置不一致结论，只允许`no_sanction`，按无处罚结束。
- `expire_review`（`open`/`suspended`/`review_pending` → `closed`）：保留期已过且复核未确认阳性时，按无处罚结束（`decision=no_sanction`，`closure=retention_expired`）。
- 样本的`result`、案件的`initial_result`和`review_result`是受保护结论：一旦记录，后到的普通更新携带不同值会被拒绝（409），初检与复检两份结论始终同时留在案件里。

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
