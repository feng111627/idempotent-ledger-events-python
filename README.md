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
| POST | `/accounts` | 创建账户：`{"account_id": "a", "initial_balance": "100.00"}` |
| GET | `/accounts/{id}` | 查询余额 |
| POST | `/transfers` | 转账：`{"from_account","to_account","amount","idempotency_key"}` |
| GET | `/replay` | 全量回放校验；`?account_id=x` 回放单个账户 |

金额一律为字符串形式的十进制数（最多两位小数），内部以整数“分”计算，
不存在浮点误差。

```bash
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"account_id":"a","initial_balance":"100.00"}'
curl -X POST localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"account_id":"b"}'
curl -X POST localhost:8000/transfers -H 'content-type: application/json' \
  -d '{"from_account":"a","to_account":"b","amount":"12.34","idempotency_key":"pay-1"}'
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

## 幂等语义

- 转账必须携带 `idempotency_key`。
- **同键 + 相同请求内容**：返回与首次完全一致的响应体（HTTP 200，响应头
  `X-Idempotent-Replay: true` 标记为重放），不重复记账、不新增事件。
- **同键 + 不同请求内容**（金额/收付款方/方向任一不同）：返回
  `409 IDEMPOTENCY_CONFLICT`，不产生任何记账。
- 请求内容以规范化 JSON 的 SHA-256 指纹存储，与键一起在同一事务内落库，
  因此并发重复提交也只记账一次。

## 失败原因分类

所有错误响应均为 `{"error": {"code", "message"}}`，底层 sqlite/框架异常
不会原样暴露：

| code | HTTP | 含义 |
| --- | --- | --- |
| `INVALID_AMOUNT` | 422 | 金额非法（非数字/负数/超过两位小数/超范围） |
| `ACCOUNT_NOT_FOUND` | 404 | 账户不存在 |
| `ACCOUNT_ALREADY_EXISTS` | 409 | 重复开户 |
| `INSUFFICIENT_FUNDS` | 422 | 余额不足 |
| `SAME_ACCOUNT_TRANSFER` | 422 | 收付款账户相同 |
| `IDEMPOTENCY_CONFLICT` | 409 | 幂等键被用于不同请求内容 |
| `STORAGE_ERROR` | 500 | 底层存储异常（已归一化，不含驱动细节） |

## 本地验证

```bash
uv run pytest
```

测试日志（`log_cli` 已开启）会打印每个用例的开始/结束及关键业务数据，
覆盖以下场景：

- `tests/test_transfer_concurrency.py` — 并发转账：不透支、不丢更新、
  总余额守恒、双向转账后回放一致。
- `tests/test_idempotency.py` — 重复提交结果一致且不重复记账、同键换内容
  返回 409、并发重复提交只记账一次、不同幂等键互不影响。
- `tests/test_replay_and_atomicity.py` — 事件回放重建余额、单账户回放、
  事件链 `balance_after` 可验证、失败转账无残留、事务中途异常整体回滚、
  驱动异常归一化。
- `tests/test_api_errors.py` — 各类失败原因的 code/HTTP 状态分类、
  未预期异常不泄露内部细节。
