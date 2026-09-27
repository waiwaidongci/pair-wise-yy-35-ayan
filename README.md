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
- `GET /api/audit`
- `GET /api/exposure_summary`：各未关闭事件的暴露人数、待通知、待随访预约与关闭阻塞项计数
- `GET /api/items/{id}/exposures`：暴露人员台账
- `POST /api/items/{id}/exposures`：按员工号登记剂量、通知方式（sms/email/phone/onsite/letter）、是否随访（同一事件同一员工仅一条，重复返回409）
- `PATCH风格 POST /api/items/{id}/exposures/{eid}`：更正剂量/通知方式/随访标记/预约
- `POST /api/items/{id}/exposures/{eid}/notify`：按当前严重度档通知本人
- `POST /api/items/{id}/exposures/{eid}/confirm`：确认本人已收到通知（必须先按当前档通知）
- `POST /api/items/{id}/exposures/{eid}/appointment`：登记医学随访预约时间
- `POST /api/items/{id}/severity`：调整严重度（需`expected_version`），原通知与确认全部作废，须按新档重新通知确认

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 暴露人员台账与关闭规则

- 台账按员工号登记剂量数值、通知方式和是否需要随访，同一员工在同一事件只有一条记录。
- 关闭事件时若仍有未完成项返回409冲突，`details`逐条列出：需要随访者尚未预约时间、剂量达到或超过调查水平（threshold）者尚未确认通知，以及事件下仍有未关闭事项。
- 严重度调整后，全部暴露人员的通知状态与确认作废，`notify_reset`审计事件记录受影响人数；随访预约保留。
- 通知方式变更同样使旧通知作废。通知、确认、随访预约/取消、台账更正和严重度调整均写入审计链；演示页展示各事件待通知与待随访人数。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
