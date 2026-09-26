# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/scheduler.py`：处置调度编排（库位占用/释放的事务承接，不含规则判定）。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：处置调度台页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/locations`：登记隔离棚库位（角色 admin/quarantine），请求体`{"code":"A-01","name":"一号格","zone":"甲区"}`。
- `GET /api/locations`：列出全部格位及当前占用（批次、处置方式、安排人、时间）。
- `GET /api/location-events`：库位台账，记录每次占格/释放的原因与操作人。
- `GET /api/dispatch-board`：调度台聚合数据（库位+批次+台账），页面一次加载。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 处置调度流程

染病批次进隔离棚由库位统一调度，替代白板记库位：

1. **先登记库位**：`POST /api/locations` 建隔离棚格位（必须先有格位才能安排批次）。
2. **批次转处置**：批次处于 `inspected` 状态执行 `quarantine` 时，`data` 必须带
   `location_code`（已登记且空闲）和 `disposal_method`
   （`incineration`/`deep_burial`/`sterilization`/`chemical`），提交后格位锁定。
3. **同格互斥**：格位清空前，另一批次再排该格返回 409，必须另选空格。
4. **释放库位**：`destroy`（销毁完成）或 `recheck`（复检转回普通检疫）都会释放
   格位；可带 `release_reason` 写明原因，留空用默认原因；原因与操作人记台账。
5. **重复提交沿用原单**：同一批次重复提交 `quarantine`（哪怕请求想换格）返回原单，
   不推进版本、不重复占格。
6. **持久化**：库位、占用和台账都存 SQLite，服务重启后状态仍在。

处置合法性由 `src/rules.py` 判定，库位记录由 `src/repository.py`/`src/scheduler.py`
承接，页面只做展示与提交；占格与状态推进在同一数据库事务内完成。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
