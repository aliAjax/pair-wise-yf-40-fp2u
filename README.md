# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验（含库位占用判断与处置方式）。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁和库位台账。
- `src/service.py`：用例编排、幂等处理、版本控制、库位联动和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：染病批次处置调度台（库位登记、占格处置、释放留痕）。
- `tests/`：完整流程、规则和失败场景测试。

## 处置调度台规则

- **先登记库位**：`POST /api/slots` 登记隔离棚格位（编号唯一），初始为空闲。
- **转处置占格**：批次 `quarantine` 时必须选择一个空闲格（`slot_id`）并记下处置方式（`disposal_method`）；库位记录批次、处置方式、占用人。
- **同格互斥**：库位未清空前不能安排第二批。规则层校验 + 库位条件 UPDATE（`WHERE status='available'`）双重兜底。
- **释放库位**：`destroy`（处置完成）或 `recheck`（复检转回普通检疫）在同一事务内释放库位，台账留下原因与操作人。
- **重复提交沿用原单**：已隔离批次再次提交 `quarantine` 直接返回原单，版本不变、不重复占格。
- 状态与台账全部落 SQLite，服务重启后仍在。

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
- `POST /api/slots`：登记库位（`code` 必填，`zone` 可选）。
- `GET /api/slots`：查看各格占用情况。
- `GET /api/slots/<id>/history`、`GET /api/slot-history`：库位占用/释放台账。

批次转处置示例：

```json
POST /api/entities/<批次id>/actions
{"action":"quarantine","data":{"pest_found":true,"sample_id":"S-1",
 "slot_id":"<库位id>","disposal_method":"incineration"},"expected_version":3}
```

处置方式：`incineration`(焚烧)、`deep_burial`(深埋)、`sterilization`(灭菌)、`chemical_treatment`(药剂)、`return_to_origin`(退回原产地)。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
