# 服务记录防重复归档

多所学校集中补录社会实践证明时，同一名志愿者的同一场服务可能以不同姓名
（含全角/空格差异）或证件尾号重复提交。直接合并会重复计时，全部拒绝又
影响真实记录。本服务保存**来源指纹、身份线索、场次证据与冲突候选**，自动
生成合并建议并由授权人员确认；误合并可拆分，迟到签到、场次取消、批量重传
均保留来源谱系；归档后只能通过更正事件调整，管理命令可反复重跑去重而结果
稳定。

## 设计要点

- **只增事件账本（SQLite）**：批次交付、记录版本、建议各代、确认/拒绝、
  拆分、签到、取消、归档、更正全部是事件，任何记录不物理删除。
- **确定性投影**：当前状态由事件序列逐字节决定，删库重放结论一致。
- **稳定标识**：记录 ID = `(来源学校, 批次, 条号)` 的哈希；来源指纹为原始
  报文规范化 JSON 的 SHA-256，键顺序不影响；去重 run_id 由算法版本与全部
  当前指纹派生，**相同数据重跑零新增事件**。
- 身份线索加权：证件尾号（3）＞手机尾号（2）＞姓名（1）；仅凭手机尾号
  撞号不连边（未成年人常共用监护人手机号）。
- 场次证据：同场馆 + 同日期 + 同场次码（大小写不敏感），或服务时间重叠。
- **人工裁决优先**：拒绝且证据未变不再提请；拆分时快照双方指纹，证据未
  再变化不重新撮合；证据修订后开放新一代建议。
- **不可变归档**：待决建议未清不能归档；归档后只能追加 `correction_applied`
  （计时更正 / 剔除重复记录 / 补挂遗漏记录 / 备注）。
- 角色边界（与 `domain/contract.json` 一致）：合并、拆分、归档、更正仅
  **文博中心运营员**；场次取消允许场馆负责人；其余角色 403。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/service_archive/`：核心服务。
  - `hashing.py`：规范化 JSON 与稳定哈希；
  - `fingerprints.py`：来源定位/指纹、身份线索、场次证据与匹配规则；
  - `events.py`：事件类型；
  - `store.py`：SQLite 只增账本（幂等键、并发安全）；
  - `projection.py`：确定性投影（状态机、谱系、人工裁决边界）；
  - `service.py`：应用服务（摄入、去重、确认/拆分/签到/取消/归档/更正）；
  - `api.py`：纯标准库 HTTP 接口；
  - `auth.py` / `clock.py` / `errors.py`：授权、时钟、错误。
- `tools/dedupe.py`：去重重跑管理命令。
- `tools/serve.py`：HTTP 服务启动入口。
- `tests/`：共 37 个回归测试（含真实回环 HTTP 与临时文件库重放）。

## 验证

```bash
# 单元与集成测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要检查
python3 tools/check_contract.py domain/contract.json

# 去重重跑（可重复执行，结果稳定）
python3 tools/dedupe.py --db service_archive.db [--json]

# 启动 HTTP 服务
python3 tools/serve.py --host 127.0.0.1 --port 8080 --db service_archive.db
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | 学校提交/重传补录批次（`source/batch_id/entries`） |
| POST | `/api/dedupe/run` | 重跑去重（管理命令亦可调用） |
| GET  | `/api/suggestions` | 待处理合并建议 |
| GET  | `/api/suggestions/<id>` | 建议各代详情 |
| POST | `/api/suggestions/<id>/confirm` | 授权确认合并 |
| POST | `/api/suggestions/<id>/reject` | 授权拒绝合并 |
| POST | `/api/groups/<id>/split` | 拆分误合并（归档前） |
| POST | `/api/records/<rid>/checkin` | 签到（迟到自动标记，补传追加不覆盖） |
| POST | `/api/sessions/cancel` | 场次取消（自动作废相关待决建议） |
| POST | `/api/groups/<id>/archive` | 归档（有待决建议时拒绝） |
| POST | `/api/groups/<id>/corrections` | 归档后更正（唯一调整通道） |
| GET  | `/api/groups`、`/api/groups/<id>` | 分组视图（状态、计时、成员指纹） |
| GET  | `/api/records/<rid>/lineage` | 一条记录的完整来源谱系 |
| GET  | `/api/batches/<source>/<batch_id>` | 批次各次传输谱系 |
| GET  | `/api/events` | 原始事件账本 |

人工操作用请求体中的 `operator` + `role` 标识；越权返回 403，状态冲突
（如归档后拆分）返回 409，领域规则冲突返回 422。

## 状态流

草拟 → 待核验（出现冲突候选）→ 已确认（运营员确认合并/拆分）→ 执行中
（签到，迟到标记保留）→ 已归档；任一场次取消 → 已取消。已归档只能通过
更正事件调整。
