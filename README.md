# 城市噪声 · 小时等效声级服务

逐分钟 A 声级按修订号上传，60 个分钟全部有效时生成小时等效声级。

```
Leq,h = 10 * log10( (1/60) * Σ_{i=0..59} 10^(L_i / 10) )
```

- FastAPI + SQLite（WAL），一个网页：上传、结果查看、JSON 查询。
- 整份上传是一个事务：要么全部生效，要么一行都不写。
- 分钟读数与其派生的小时结果在同一次提交内写入，跨进程并发也不会出现半批数据或过期结果。
- Docker Compose 一键启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `noise-data` 的 `/data/noise.sqlite3`。端口可用 `NOISE_PORT` 覆盖：

```bash
NOISE_PORT=9000 docker compose up --build
```

### 两个服务进程共库

需求要求两个服务进程并发写出同一份数据。Compose 中的 `web-b` 是一个可选的第二副本，
与 `web` 共享同一个数据卷（即同一个 SQLite 文件），默认不启动：

```bash
docker compose --profile dual up --build
# web   -> http://localhost:8000
# web-b -> http://localhost:8001
```

两个进程之间没有应用层缓存、没有本地内存状态：每次请求都现开 SQLite 连接，
写操作使用 `BEGIN IMMEDIATE`（跨进程串行），读操作用单个读事务取 WAL 快照。

> SQLite 的 WAL 依赖同一主机上的普通文件系统。命名卷（local driver）满足该条件；
> 不要把数据库放在 NFS/SMB 卷上。

## 上传格式

`POST /api/upload`，CSV，UTF-8，表头可选：

```csv
minute,revision,value
0,1,52.3
1,1,51.8
2,1,
...
```

| 列 | 说明 |
| --- | --- |
| `minute` | 0–59 的整数；同一文件内不得重复（缺的分钟就是“缺测”） |
| `revision` | 正整数修订号，按分钟单调递增 |
| `value` | 有限 dB 数值（±1000 以内，按 0.01 dB 存储）；留空 = 无效 |
| `hour` | 可选列；有则必须与提交小时一致，没有则用表单/JSON 的小时 |

无效标记除空格外还接受 `invalid` / `n/a` / `null` / `missing`（大小写不敏感）。
列名大小写不敏感，可乱序；也接受 4 列以上（多余列忽略）。

小时写法：`2026-11-15T09:00Z`（也接受 `2026-11-15 09:00+00:00`、`...T11:00+02:00`，
统一按 UTC 归档，必须整点）。

提交方式（任选其一）：

```bash
# 1) multipart 文件
curl -F hour=2026-11-15T09:00Z -F file=@hour.csv http://localhost:8000/api/upload
# 2) multipart 文本
curl -F hour=2026-11-15T09:00Z -F text="$(cat hour.csv)" http://localhost:8000/api/upload
# 3) JSON
curl -H 'content-type: application/json' \
     -d '{"hour":"2026-11-15T09:00Z","csv":"minute,revision,value\n0,1,52.3\n"}' \
     http://localhost:8000/api/upload
# 4) text/csv 正文 + ?hour=
curl -H 'content-type: text/csv' --data-binary @hour.csv \
     'http://localhost:8000/api/upload?hour=2026-11-15T09:00Z'
```

### 修订规则（整份上传原子）

| 情况 | 结果 |
| --- | --- |
| 该分钟没有旧读数 | 接受 |
| 修订号 > 已记录 | 接受，替换 |
| 修订号 = 已记录 且 内容相同 | 接受但不改动（安全重传） |
| 修订号 = 已记录 且 内容不同 | **整份上传失败**（409） |
| 修订号 < 已记录 | **整份上传失败**（409） |

内容比较按存储精度（0.01 dB）进行，所以 `50.001` 与已记录的 `50.00` 视为同一内容。
任何一行违规，本次上传的所有行都不写入；响应里给出逐条原因。
数据库短暂繁忙时返回 503（`retryable: true`），表示**未写入**，可以直接重试。

## 查询

- `GET /api/hours` — 已登记小时列表及是否已发布。
- `GET /api/hours/{hour}` — 该小时的分钟修订、小时结果与当前状态：

```json
{
  "ok": true,
  "hour": "2026-11-15T09:00Z",
  "status": "published",
  "leq_db": 52.23,
  "computed_at": "2026-11-15T12:05:13.082963Z",
  "submission_id": 1,
  "computed_from_revisions": {"0": 1, "1": 1, "...": 1},
  "minute_revisions": {"0": 1, "1": 1, "...": 1},
  "minutes": [{"minute": 0, "revision": 1, "invalid": false, "db": 52.3}],
  "invalid_minutes": [],
  "missing_minutes": [],
  "minutes_recorded": 60,
  "valid_minutes": 60,
  "last_event": {"action": "published", "leq_db": 52.23, "submission_id": 1,
                 "created_at": "2026-11-15T12:05:13.082963Z"}
}
```

- `status` 为 `published` 时 `leq_db` 是当前小时声级；
  为 `unavailable` 时 `leq_db` 为 `null`，`invalid_minutes` / `missing_minutes`
  列出导致缺失的分钟。
- `computed_from_revisions` 就是网页展示的“所用修订”，它与 `minute_revisions`
  永远一致——两者在同一个事务/快照里读取。
- `last_event` 记录最近一次发布或撤销（新无效修订导致撤销后仍能查到曾经撤销）。

## 撤销与恢复

- 已发布小时 + 新的无效修订 → 结果行删除，小时回到 `unavailable`，
  `invalid_minutes` 列出该分钟；网页明确显示“已撤销 / 未发布”和无效、缺测分钟。
- 之后再提交更高修订的有效值 → 60 分钟齐全时自动重新发布。
- 撤销/恢复都发生在提交事务内，和读数一起提交。

## 一致性设计

1. 写入：`BEGIN IMMEDIATE` 一次事务内完成“校验全部行 → 写 submissions →
   upsert minutes → 计算并写/删 hourly_results → 记录事件”。SQLite 对跨进程写者
   串行化，因此两个服务进程的提交不会交错。
2. 读取：所有 SELECT 在同一个读事务内完成（第一条 SELECT 固定 WAL 快照），
   读到的是某一次提交后的完整状态。
3. 结果与读数同源：`hourly_results.revisions_json` 保存计算时的修订集合，
   查询时若与 `minutes` 不一致会直接报错而不是返回可疑数据。
4. 幂等：同号同内容重传不改变 `computed_at`、`submission_id` 与结果，可安全重试。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
NOISE_DB_PATH=/tmp/noise.sqlite3 uvicorn app.main:app --reload
pytest -q
```

测试覆盖：

- `tests/test_rules.py` — CSV 解析、小时/修订/数值校验、Leq 公式数值。
- `tests/test_api.py` — 修订规则、整份拒绝、撤销/恢复、查询与页面。
- `tests/test_multiprocess.py` — 启动**两个真实 uvicorn 进程**共享一个数据库文件：
  并发提交、并发冲突、并发撤销，同时一个读线程持续校验“读到的 Leq 必须等于
  读到的分钟值重算结果”，并直接读 SQLite 文件核对落库内容。

`tests/serve_worker.py` 是被测试拉起的单进程 worker（stdin 关闭即退出）。

## 目录

```
app/rules.py        CSV 解析、修订语义、Leq 公式
app/storage.py      SQLite 事务、快照读取、结果发布/撤销
app/main.py         FastAPI 路由与错误映射
app/static/index.html  单页网页（上传 / 结果 / JSON）
Dockerfile, docker-compose.yml
```
