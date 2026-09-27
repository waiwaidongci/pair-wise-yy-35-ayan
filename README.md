# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/exposures`
- `POST /api/items/{id}/exposures`，按员工号登记剂量数值、通知方式和是否需要随访，同一员工仅一条记录
- `POST /api/items/{id}/exposures/{employee_id}/notify`
- `POST /api/items/{id}/exposures/{employee_id}/confirm`
- `POST /api/items/{id}/exposures/{employee_id}/follow-up`，提交`appointment`（ISO 8601时间）
- `POST /api/items/{id}/severity`，必须提交`expected_version`
- `GET /api/exposures/summary`，返回待通知与待随访人数
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。暴露人员台账中，需随访的人没有预约时间、或剂量达到调查水平的人尚未确认时，关闭事件返回冲突并列出未完成项；严重度调整后原通知与确认作废，须按新档重新通知和确认；登记、通知、确认、随访与调档全部进入审计链。演示页实时显示待通知与待随访人数。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
