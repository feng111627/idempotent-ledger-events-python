# Idempotent Ledger Events

本地账户与资金事件账本服务（FastAPI + SQLite）。目标：在并发转账、重复提交
和历史回放场景下，账务结果始终可解释、可复现。

## 快速开始

```bash
uv sync                 # 安装依赖
uv run python main.py   # 启动服务（127.0.0.1:8000，数据落盘 ledger.db）
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 创建账户：`{"account_id": "a", "initial_balance": "100.00", "idempotency_key": "open-1"}`（key 可选） |
| GET | `/accounts/{id}` | 查询余额 |
| POST | `/transfers` | 转账：`{"from_account","to_account","amount","idempotency_key"}` |
| POST | `/transfers/{transfer_id}/reversal` | 全额冲正：`{"idempotency_key":"rev-1"}` |
| GET | `/replay` | 全量回放校验；`?account_id=x` 回放单个账户 |

金额一律为字符串形式的十进制数（最多两位小数），内部以整数“分”计算，
不存在浮点误差。

```bash
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"account_id":"a","initial_balance":"100.00","idempotency_key":"open-a"}'
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"account_id":"b"}'
curl -X POST localhost:8000/transfers -H 'content-type: application/json' \
  -d '{"from_account":"a","to_account":"b","amount":"12.34","idempotency_key":"pay-1"}'
# 用上一步返回的 transfer_id 发起全额冲正，钱退回 a
curl -X POST localhost:8000/transfers/<transfer_id>/reversal \
  -H 'content-type: application/json' -d '{"idempotency_key":"rev-1"}'
curl localhost:8000/replay
```

## 冲正（退款）语义

- **冲什么**：只能对一笔**已经成功**的转账发起**全额**冲正，把钱退回原
  付款方；不支持部分冲正。
- **只能冲几次**：同一笔转账**最多冲正一次**。
- **追加记录，不改历史**：冲正不修改、不删除原转账的任何事件，而是追加
  两条事件（收款方 `reversal_debit` 出账、付款方 `reversal_credit` 入账）
  和一条 `reversals` 记录。`GET /replay` 既能还原原始转账，也能在
  `reversals` 列表与每笔转账的 `reversed` 标记中看到对应冲正。
- **重复提交**：同一提交标识重试冲正，返回与首次完全一致的响应（带头
  `X-Idempotent-Replay: true`），不重复记账。
- **并发处理**：写事务经 `BEGIN IMMEDIATE` 串行化，且 `reversals.transfer_id`
  有唯一约束。同 key 并发只有一次真正记账，其余全部幂等重放；不同 key
  并发冲正同一笔，只有一个成功，其余得到 `TRANSFER_ALREADY_REVERSED`。
  不会重复退款、多退。
- **不透支**：冲正前若收款方当前余额不足以退回，冲正被拒绝
  （`INSUFFICIENT_FUNDS`），任何账户都不会被冲成负数。
- **失败原因可区分**：目标转账不存在 → `404 TRANSFER_NOT_FOUND`；
  该转账已经被冲正过 → `409 TRANSFER_ALREADY_REVERSED`。

## 幂等语义：提交标识的作用范围

提交标识可以用于**所有写操作**（开户、转账、冲正；开户为可选携带）。
标识的作用域是 **操作 + 业务对象**：

| 操作 | 业务对象（scope） | 请求内容（指纹） |
| --- | --- | --- |
| 开户 `create_account` | 账户 `account_id` | 初始余额 |
| 转账 `transfer` | 付款方→收款方 这一对账户 | 金额 |
| 冲正 `reverse_transfer` | 被冲正的 `transfer_id` | （全额，无额外内容） |

- **同操作 + 同对象 + 同内容**：返回首次保存的响应（HTTP 200/201，头
  `X-Idempotent-Replay: true`），不重复记账、不新增事件。
- **同操作 + 同对象 + 内容不同**：`409 IDEMPOTENCY_CONFLICT`，不产生任何
  记账，也不改写已保存的首次结果。
- **同标识用于不同操作、或不同业务对象**：各算各的，互不覆盖、也不会被
  误判成冲突。例如同一个 key 开两个不同账户、或对不同账户对转账，都是
  独立的业务请求。
- 请求内容以规范化 JSON 的 SHA-256 指纹存储，与 `(操作, 对象类型, 对象
  标识, key)` 一起在同一事务内落库，因此并发重复提交也只记账一次。

## 边界行为

- **开户零金额**：`0`、`0.0`、`0.00`、`000`、JSON 数字 `0` / `0.0` 等
  常见零金额写法都会被归一化为 `0.00` 放行；零初始金额不产生资金事件。
  转账金额仍然必须为正数，`0` 一律按 `INVALID_AMOUNT` 拒绝。
- **非法请求体**：字段缺失、类型不对、空字符串、甚至整个 body 不是合法
  JSON，都返回与业务错误**完全一致**的结构
  `{"error":{"code":"INVALID_REQUEST","message":...}}`（HTTP 422），
  不会冒出框架默认的报错结构。

## 一致性保证

- **原子性**：一次转账的全部写入（双方余额更新 + 借贷两条事件 + 幂等记录）
  在同一个 SQLite 事务（`BEGIN IMMEDIATE`）内完成，要么全部提交、要么全部
  回滚。写入中途异常不会产生“事件与余额只落一边”的状态（有对应单测模拟
  第二条事件写入时崩溃并验证回滚）。
- **不透支**：业务层校验余额，数据库层另有
  `CHECK (balance_cents >= 0)` 约束兜底。
- **不丢更新 / 并发安全**：写事务经 `BEGIN IMMEDIATE` 串行化，连接设置
  `busy_timeout=30s`，写锁竞争时等待而非报错；文件库开启 WAL。
- **总余额守恒**：每笔转账一借一贷、同额异号记账，系统内总余额不变；
  冲正同样是一借一贷同额异号的追加事件，守恒不被破坏。
- **可回放**：所有资金变动（含开户初始入金）都是追加事件，记录
  `delta_cents` 与 `balance_after_cents`。`GET /replay` 从事件流重建余额，
  与当前余额逐账户比对，并校验事件链上每条 `balance_after` 与重放运行
  余额一致，输出 `consistent` 结论。

## 幂等语义

提交标识适用于开户、转账、冲正全部写操作，按“操作 + 业务对象”划分作用域，
详见上文 [幂等语义：提交标识的作用范围](#幂等语义提交标识的作用范围)。

## 失败原因分类

所有错误响应均为 `{"error": {"code", "message"}}`，底层 sqlite/框架异常
不会原样暴露：

| code | HTTP | 含义 |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | 请求体不合法（字段缺失/类型不对/非法 JSON） |
| `INVALID_AMOUNT` | 422 | 金额非法（非数字/零或负数用于转账/超过两位小数/超范围） |
| `ACCOUNT_NOT_FOUND` | 404 | 账户不存在 |
| `ACCOUNT_ALREADY_EXISTS` | 409 | 重复开户 |
| `INSUFFICIENT_FUNDS` | 422 | 余额不足（转账透支或冲正时收款方余额不足） |
| `SAME_ACCOUNT_TRANSFER` | 422 | 收付款账户相同 |
| `TRANSFER_NOT_FOUND` | 404 | 冲正目标转账不存在 |
| `TRANSFER_ALREADY_REVERSED` | 409 | 目标转账已经被冲正过 |
| `IDEMPOTENCY_CONFLICT` | 409 | 同操作同对象下，幂等键被用于不同请求内容 |
| `STORAGE_ERROR` | 500 | 底层存储异常（已归一化，不含驱动细节） |

## 本地验证

```bash
uv run pytest                       # 全量单测（-v + log_cli，逐用例打印）
uv run pytest tests/test_reversal_concurrency.py   # 只跑冲正并发
uv run python main.py               # 起服务，可用上文 curl 手工验证
```

测试日志（`log_cli` 已开启）会打印每个用例的开始/结束及关键业务数据，
覆盖以下业务：

- `tests/test_transfer_concurrency.py` — 并发转账：不透支、不丢更新、
  总余额守恒、双向转账后回放一致（**既有，未回归**）。
- `tests/test_idempotency.py` — 重复提交结果一致且不重复记账、同操作同
  对象换内容返回 409、并发重复提交只记账一次、同键用于不同业务对象互不
  影响（**既有**）。
- `tests/test_replay_and_atomicity.py` — 事件回放重建余额、单账户回放、
  事件链 `balance_after` 可验证、失败转账无残留、事务中途异常整体回滚、
  驱动异常归一化（**既有，未回归**）。
- `tests/test_api_errors.py` — 各类失败原因的 code/HTTP 状态分类、
  未预期异常不泄露内部细节（**既有，未回归**）。
- `tests/test_reversal.py` — **新增**：全额冲正退款、原转账不改不删且
  回放可见、冲正重复提交幂等、转账不存在 / 已冲正两类错误可区分、
  余额不足时冲正不产生负数、HTTP 层冲正与重放头。
- `tests/test_reversal_concurrency.py` — **新增**：同 key 并发冲正只记账
  一次、不同 key 并发冲正只有一个成功、多轮并发无竞态。
- `tests/test_idempotency_scope.py` — **新增**：开户带提交标识幂等、
  并发开户只开一次、同键不同账户 / 跨操作各算各的、同键同账户换初始
  金额为真冲突。
- `tests/test_boundaries.py` — **新增**：`0`/`0.0`/`0.00` 等零金额写法
  统一放行、转账零金额仍拒绝、字段缺失 / 类型不对 / 非法 JSON 一律返回
  统一的 `INVALID_REQUEST` 错误结构。
