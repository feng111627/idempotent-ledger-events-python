# 本地幂等资金事件账本（FastAPI）

一个本地运行的账户与资金事件账本服务，目标是让**并发转账、重复提交、历史回放**
下的账务结果始终可解释、可复现：

- 并发转账不透支、不丢更新、总余额守恒；
- 金额全程整数最小单位运算，无浮点误差；
- 幂等键重复请求返回首次一致结果且不重复记账，同键换内容明确拒绝；
- 每次资金变动以「追加事件」落库，可据事件流回放重建并核对余额；
- 事件与余额在单个事务内同生共死，底层驱动异常统一归一化。

## 目录结构

```
app/domain.py       金额值对象、账户/事件领域类型
app/errors.py       归一化错误分类
app/db.py           SQLite 连接（每线程独立连接 / WAL）与建表
app/repository.py   仓储：单事务原子写「事件 + 余额 + 幂等记录」
app/service.py      业务：开户、查询、转账、幂等、事件回放
app/schemas.py      HTTP 入参模型（十进制金额字符串校验）
app/api.py          路由与统一错误信封
main.py             应用组装入口
tests/              账户 / 幂等 / 并发 / 回放 / 错误归一化 单测
docs/consistency.md 一致性、幂等语义与失败分类的详细说明
```

## 快速开始

```bash
uv sync --group dev
uv run uvicorn main:app --reload          # 默认库文件 ledger.db
# 或指定库路径
LEDGER_DB_PATH=/tmp/my-ledger.db uv run uvicorn main:app
```

交互式文档：<http://127.0.0.1:8000/docs>

## API

金额一律使用**十进制字符串**收发（如 `"12.30"`），服务端用 `Decimal`
精确解析后立即转为整数「最小货币单位（分）」存储运算，最多 2 位小数。

### 开户

```bash
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "account_id": "A", "opening_balance": "100.00", "idempotency_key": "open-A"
}'
# 201 -> {"account_id":"A","balance":"100.00","balance_minor":10000,"replayed":false}
```

`idempotency_key` 可选。

### 余额查询

```bash
curl -s localhost:8000/accounts/A
# {"account_id":"A","balance":"100.00","balance_minor":10000}
```

### 转账（必须带幂等键）

```bash
curl -s -X POST localhost:8000/transfers -H 'Content-Type: application/json' -d '{
  "from_account_id":"A","to_account_id":"B",
  "amount":"30.00","idempotency_key":"tx-20261006-001"
}'
```

- 用**相同幂等键 + 相同内容**重复提交：返回与首次一致的结果，
  响应里 `"replayed": true`，且只记账一次；
- 用**相同幂等键但不同内容**（改金额/方向/操作类型）：返回
  `409 idempotency_conflict`，绝不静默覆盖。

### 事件流与回放

```bash
curl -s "localhost:8000/events"                 # 全部追加事件
curl -s "localhost:8000/events?account_id=A"    # 单账户事件
curl -s -X POST localhost:8000/replay           # 按事件流重建并核对
curl -s -X POST "localhost:8000/replay?account_id=A"
```

`/replay` 只依据追加事件重算余额，逐层校验「事件链自洽 + 与账户表一致 +
总额守恒」，不一致返回 `500 replay_mismatch`。

## 本地验证

```bash
uv sync --group dev
uv run pytest                       # 全量单测，日志带业务场景与逐用例结果
uv run pytest -k concurrent         # 只跑并发转账
uv run pytest -k idempotency        # 只跑幂等
uv run pytest -k replay             # 只跑回放一致性
```

测试日志会打印每个业务场景的执行情况，例如：

```
[密集双向转账] 200 笔全部成功，A=940000 B=1060000 总额=2000000（初始 2000000）
[热点入账] 20 线程各转 3.33，HUB 余额=6660（期望 6660）
[超额抢兑] 成功=2 因余额不足失败=8（期望成功 2）
[同键并发] 首次落账=1 回放=11 冲突=0，A=990.00 B=10.00
[原子性] 透支回滚后事件数不变(2)，总额仍为 10.00
[持久化重启] 重连后重放：A=38.00 B=62.00 总额守恒，事件数=4
```

## 设计要点（详见 docs/consistency.md）

- 金额：整数最小单位 + 边界 `Decimal` 解析，杜绝 `0.1+0.2` 类误差；
- 并发：每工作线程独立 SQLite 连接，写事务 `BEGIN IMMEDIATE` 串行化写者，
  扣款为带 `balance >= amount` 的条件更新，数据库层兜底防透支/丢更新；
- 原子性：一笔转账在单事务内完成「条件扣款 + 入账 + 出/入两条事件 +
  幂等记录」，任何一步失败整体回滚；
- 幂等：权威查重置于写事务内，并发同键也只可能有一笔落库；
- 错误归一化：驱动异常只在服务端日志记录，对外统一为有业务含义的错误码。
