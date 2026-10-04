# 服务记录防重复归档

学校集中补录社会实践证明时，同一名志愿者的同一场服务可能以**不同姓名或证件尾号**被多所学校重复提交。本服务保存来源指纹、身份线索、场次证据与冲突候选，自动生成**确定性**合并建议，由授权人员（文博中心运营员 / 场馆负责人）确认：

- 直接合并会重复计时 → 只有主记录计时，重复记录仅保留谱系；
- 全部拒绝又影响真实记录 → 系统先给建议，人工再裁决。

## 领域约束（不变量）

1. **来源指纹谱系**：每份材料按内容计算 SHA-256 指纹；完全相同材料的批量重传幂等识别，不产生新记录、不重复计时，仅累加 `transmit_seq` 并写入 `batch.retransmit` 事件。
2. **身份候选匹配**：姓名（0.4）、证件尾号（0.4）、同一场次（0.2）打分；同场次且总分 ≥ 0.6、且姓名或尾号至少一项完全一致才成案；弱线索（仅模糊姓名、尾号冲突）不成案。
3. **人工合并确认**：建议状态为 `proposed`，仅授权岗位可 `confirm` / `reject`。驳回的记录对进入 `blocked_pairs`，**重跑永不再成案，也不会经第三个顶点传递复合**。
4. **归档更正事件**：归档后记录只读，唯一调整路径是追加 `record.corrected` 事件（白名单字段、记录前后值），历史谱系不被覆盖。

误合并拆分（`merge.split`）、迟到签到（`record.late_checkin`）、场次取消（`session.cancelled`）、批量重传（`batch.retransmit`）均为追加事件，完整保留来源谱系。事件流为 prev/hash 哈希链，可通过 `verify-chain` 发现篡改。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/service_records/`：服务端实现
  - `canonical.py` 规范化序列化与指纹；`models.py` 状态/事件；`auth.py` 角色授权；
  - `matching.py` 纯函数式线索比对与约束聚类（结果稳定）；
  - `store.py` SQLite 持久化 + 追加式哈希链事件流；
  - `service.py` 用例服务层；`httpapi.py` JSON HTTP 接口；`manage.py` 管理命令。
- `tools/check_contract.py`：契约命令行检查。
- `examples/`：批量导入样例与令牌样例。
- `tests/`：契约、用例（30 项）、HTTP、CLI 回归测试。

仅依赖 Python 3.11+ 标准库（含 `sqlite3`）。

## 快速开始（管理命令）

```bash
export PYTHONPATH=src

# 1. 初始化库
python3 -m service_records.manage --db data/app.db init-db

# 2. 导入学校补录材料（JSONL，或含 submissions 的 JSON）
python3 -m service_records.manage --db data/app.db import \
  --file examples/submissions.jsonl --actor op_zhang --role operator

# 3. 重跑去重（可反复执行：结果稳定，已存在的建议跳过，驳回对不再出现）
python3 -m service_records.manage --db data/app.db dedup \
  --actor op_zhang --proposals
python3 -m service_records.manage --db data/app.db dedup --actor op_zhang

# 4. 查看候选 / 谱系 / 校验哈希链
python3 -m service_records.manage --db data/app.db list candidates
python3 -m service_records.manage --db data/app.db lineage --record <rec_id>
python3 -m service_records.manage --db data/app.db verify-chain
```

其他命令：`verify --record`（核验无冲突记录）、`list records [--status ...]`，
HTTP 服务：`serve --host 127.0.0.1 --port 8080 --tokens examples/tokens.json`。

## HTTP 接口

鉴权：`Authorization: Bearer <token>`（令牌文件见 `examples/tokens.json`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/submissions` | 登记一份材料（相同指纹=重传，幂等） |
| POST | `/v1/submissions/batch` | 批量登记 |
| POST | `/v1/dedup-runs` | 重跑去重识别，返回本次新增/跳过数与输入签名 |
| GET | `/v1/candidates?status=proposed` | 冲突候选与评分理由 |
| POST | `/v1/candidates/{id}/confirm` | 授权确认合并（主记录计时） |
| POST | `/v1/candidates/{id}/reject` | 授权拒绝（记录对永久阻断） |
| POST | `/v1/records/{id}/verify` | 核验无冲突记录 |
| POST | `/v1/records/{id}/split` | 拆分误合并（可指定 `release_ids`） |
| POST | `/v1/records/{id}/late-checkin` | 补登迟到签到 |
| POST | `/v1/sessions/{code}/cancel` | 场次取消（归档记录自动跳过） |
| POST | `/v1/archive` | 归档已确认主记录 |
| POST | `/v1/records/{id}/corrections` | 归档后更正（唯一调整路径） |
| GET | `/v1/records/{id}/lineage` | 来源指纹、重传计数与完整事件谱系 |

合并确认后，重复记录 `merged_into` 指向主记录、不单独计时；拆分后释放的记录恢复
`received`，原候选置 `superseded`，相关记录对加入阻断表。

## 重跑为什么稳定

- 记录号 `rec_<hash>`、候选号 `cand_<hash>` 均由规范化内容派生，与到达顺序无关；
- 匹配是纯函数：同场次分组 → 确定性打分 → 按**分数降序、编号升序**贪心约束聚类，
  合并后若簇内出现阻断对则拒绝该合并；
- 每次运行记录 `input_sig`（活动记录指纹 + 阻断对的内容签名）：输入不变，签名不变，
  输出的候选集合不变；已存在候选跳过，不产生重复事件。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 契约 + 用例 + HTTP + CLI
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
