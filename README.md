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
| POST | `/accounts` | 创建账户：`{"account_id": "a", "initial_balance": "100.00"}`，可选 `"idempotency_key"` |
| GET | `/accounts/{id}` | 查询余额 |
| POST | `/transfers` | 转账：`{"from_account","to_account","amount","idempotency_key"}` |
| POST | `/transfers/{id}/reversal` | 冲正：`{"idempotency_key"}`，全额退回原付款方 |
| GET | `/replay` | 全量回放校验；`?account_id=x` 回放单个账户 |

金额一律为字符串形式的十进制数（最多两位小数），内部以整数“分”计算，
不存在浮点误差。开户初始金额允许零，`"0"`、`"0.0"`、`"0.00"` 等写法
都解析为同一个合法值 0；转账金额仍必须为正。

```bash
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"account_id":"a","initial_balance":"100.00","idempotency_key":"open-a"}'
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"account_id":"b"}'
curl -X POST localhost:8000/transfers -H 'content-type: application/json' \
  -d '{"from_account":"a","to_account":"b","amount":"12.34","idempotency_key":"pay-1"}'
# 对上面返回的 transfer_id 发起全额冲正
curl -X POST localhost:8000/transfers/<transfer_id>/reversal \
  -H 'content-type: application/json' -d '{"idempotency_key":"rev-1"}'
curl localhost:8000/replay
```

## 一致性保证

- **原子性**：一次转账的全部写入（双方余额更新 + 借贷两条事件 + 幂等记录）
  在同一个 SQLite 事务（`BEGIN IMMEDIATE`）内完成，要么全部提交、要么全部
  回滚。写入中途异常不会产生“事件与余额只落一边”的状态（有对应单测模拟
  第二条事件写入时崩溃并验证回滚）。
- **不透支**：业务层校验余额，数据库层另有
  `CHECK (balance_cents >= 0)` 约束兜底。
- **不丢更新 / 并发安全**：写事务经 `BEGIN IMMEDIATE` 串行化，连接设置
  `busy_timeout=30s`，写锁竞争时等待而非报错；文件库开启 WAL。
- **总余额守恒**：每笔转账一借一贷、同额异号记账，系统内总余额不变。
- **可回放**：所有资金变动（含开户初始入金）都是追加事件，记录
  `delta_cents` 与 `balance_after_cents`。`GET /replay` 从事件流重建余额，
  与当前余额逐账户比对，并校验事件链上每条 `balance_after` 与重放运行
  余额一致，输出 `consistent` 结论。

## 冲正语义

- **能冲什么**：任何一笔已成功、且尚未被冲正过的转账，可发起一次
  **全额** 冲正，款项退回原付款方（不支持部分冲正）。
- **只能冲一次**：`reversals` 表以原 `transfer_id` 为主键，在同一事务内
  登记，同一笔转账最多冲正一次。
- **追加不改写**：冲正以两条新事件（原收款方 debit、原付款方 credit）
  落库，事件的 `reversal_of` 指向原始 `transfer_id`；原始转账事件永不
  改写或删除。`GET /replay` 的 `reversals` 段会列出
  “原转账 -> 冲正”的对应关系，重建余额与当前余额依然对得上。
- **重复提交**：冲正按 `reversal:{transfer_id}` 作用域幂等。同一
  `idempotency_key` 重试返回与首次完全一致的结果（响应头
  `X-Idempotent-Replay: true`），不重复记账。
- **并发**：并发冲正由 `BEGIN IMMEDIATE` 串行化——同键并发只有一个真正
  记账、其余幂等重放；换键并发只有一个成功、其余得到
  `409 TRANSFER_ALREADY_REVERSED`。任何情况下都不会多退或把余额冲成负数。
- **失败原因可区分**：转账不存在 -> `404 TRANSFER_NOT_FOUND`；
  已被冲正过 -> `409 TRANSFER_ALREADY_REVERSED`；收款方余额不足以退回
  （钱已被转走）-> `422 INSUFFICIENT_FUNDS`，冲正不落任何残留。

## 幂等语义

- 转账必须携带 `idempotency_key`；开户、冲正可选（冲正接口必填）。
- **作用域**：幂等记录按 `(作用域, 键)` 唯一，作用域 = 操作 + 业务对象
  （开户为 `account:{account_id}`，冲正为 `reversal:{transfer_id}`，
  转账的业务对象即键本身）。因此同一个标识用在不同操作或不同业务对象上
  **各算各的**，互不覆盖、不误判冲突。
- **同作用域同键 + 相同请求内容**：返回与首次完全一致的响应体（响应头
  `X-Idempotent-Replay: true` 标记为重放），不重复记账、不新增事件。
- **同作用域同键 + 不同请求内容**（金额/收付款方/方向/初始余额任一不同）：
  返回 `409 IDEMPOTENCY_CONFLICT`，不产生任何记账。
- 请求内容以规范化 JSON 的 SHA-256 指纹存储，与键一起在同一事务内落库，
  因此并发重复提交也只记账一次。

## 请求边界行为

- **零金额**：开户的 `initial_balance` 接受 `"0"`、`"0.0"`、`"0.00"` 等
  零金额写法，统一解析为 0，不产生入金事件；负数和转账的零金额仍被
  `422 INVALID_AMOUNT` 拒绝。
- **非法请求体**：字段缺失、类型不对、JSON 无法解析等校验失败，统一返回
  `422 {"error": {"code": "INVALID_REQUEST", "message": ...}}`，
  结构与所有业务错误完全一致，不会出现框架默认的其它错误格式。

## 失败原因分类

所有错误响应均为 `{"error": {"code", "message"}}`，底层 sqlite/框架异常
不会原样暴露：

| code | HTTP | 含义 |
| --- | --- | --- |
| `INVALID_REQUEST` | 422 | 请求体不合法（字段缺失/类型错误/JSON 无法解析） |
| `INVALID_AMOUNT` | 422 | 金额非法（非数字/负数/超过两位小数/超范围） |
| `ACCOUNT_NOT_FOUND` | 404 | 账户不存在 |
| `ACCOUNT_ALREADY_EXISTS` | 409 | 重复开户（未带提交标识时） |
| `INSUFFICIENT_FUNDS` | 422 | 余额不足（含冲正时收款方已无力退回） |
| `SAME_ACCOUNT_TRANSFER` | 422 | 收付款账户相同 |
| `TRANSFER_NOT_FOUND` | 404 | 要冲正的转账不存在 |
| `TRANSFER_ALREADY_REVERSED` | 409 | 该转账已被冲正过 |
| `IDEMPOTENCY_CONFLICT` | 409 | 同作用域同幂等键被用于不同请求内容 |
| `STORAGE_ERROR` | 500 | 底层存储异常（已归一化，不含驱动细节） |

## 本地验证

```bash
uv run pytest
```

测试日志（`log_cli` 已开启）会打印每个用例的开始/结束及关键业务数据，
覆盖以下场景：

- `tests/test_transfer_concurrency.py` — 并发转账：不透支、不丢更新、
  总余额守恒、双向转账后回放一致。
- `tests/test_idempotency.py` — 转账重复提交结果一致且不重复记账、同键换
  内容返回 409、并发重复提交只记账一次、不同幂等键互不影响。
- `tests/test_idempotency_scope.py` — 开户幂等重试、同标识跨操作/跨账户
  各自独立、同操作同对象换内容才冲突、并发同键开户只创建一次。
- `tests/test_reversal.py` — 冲正全额退款且历史只追加不改写、回放可见
  原转账与冲正的对应关系、重复提交幂等、并发冲正只退一次、转账不存在与
  已冲正是可区分的错误、收款方余额不足时冲正干净失败、失败无残留。
- `tests/test_request_boundaries.py` — 零金额各写法放行开户、转账零金额
  仍拒绝、非法请求体（缺字段/类型错/坏 JSON）与业务错误同一结构。
- `tests/test_replay_and_atomicity.py` — 事件回放重建余额、单账户回放、
  事件链 `balance_after` 可验证、失败转账无残留、事务中途异常整体回滚、
  驱动异常归一化。
- `tests/test_api_errors.py` — 各类失败原因的 code/HTTP 状态分类、
  未预期异常不泄露内部细节。
