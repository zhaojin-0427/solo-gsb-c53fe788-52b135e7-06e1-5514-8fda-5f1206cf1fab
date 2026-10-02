# 越野赛芯片计时裁决 API

基于 **Python + FastAPI + PostgreSQL** 的越野赛芯片计时裁决服务：

- **不可变赛道**：创建时填写 UTC 发枪时间、按顺序经过的至少一个检查点、终点、正整数圈数，以及
  起点→首点、各相邻点、末点→终点的通行秒数区间（`0 < min ≤ max`）。终点即下一圈起点。
  赛道创建后不可修改（不提供任何更新/删除接口）。
- **芯片读数上传**：每条读数带全局唯一 ID、点位、UTC 时间，允许乱序补传；
  同 ID 同内容幂等返回，同 ID 异内容返回 `409`；误读只能通过追加引用该 ID 的作废事件来作废。
- **裁决**：从发枪时间起逐点选取最多指定圈数的合法过点链 —— 先最大化过点数，
  再选最早末点时间，最后按整链 ID 字典序破同分。完成全部圈数才算完赛，
  返回用时、采纳读数与排除读数。
- **版本化发布**：按芯片发布裁决时，在同一事务中原子固定接收序号高水位与赛道快照，
  生成不可变版本；后续补传或作废不会改写旧版本。发布与事件入库按芯片互斥，
  赛道快照、接收序号高水位与裁决输入始终对应同一原子边界：边界前提交的该芯片事件
  全部进入该版，边界后提交的事件序号必然高于旧版高水位、只能进入后续版本。
  以预期版本号重裁决生成新版本，版本冲突返回 `409`。

## 目录结构

```
├── docker-compose.yml      # 一键启动：PostgreSQL 16 + API
├── Dockerfile
├── requirements.txt        # 运行依赖（锁定版本）
├── requirements-dev.txt    # 测试依赖
├── app/
│   ├── main.py             # FastAPI 路由 + 发布裁决事务逻辑
│   ├── adjudication.py     # 纯函数裁决算法（不依赖数据库）
│   ├── models.py           # SQLAlchemy 模型（共享接收序号序列等）
│   ├── schemas.py          # Pydantic 请求校验
│   └── db.py               # 引擎/会话
├── tests/
│   ├── test_adjudication.py# 裁决算法单元测试（无需数据库）
│   └── test_api.py         # API 集成测试（需要 PostgreSQL，含并发边界回归测试）
└── scripts/
    └── smoke.sh            # 端到端冒烟脚本（即本文档示例流程）
```

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000/health` | 健康检查 |
| `http://localhost:8000/docs` | Swagger 交互式文档 |
| `http://localhost:8000/` | 服务索引 |

PostgreSQL 暴露在 `localhost:5432`（用户/密码/库均为 `timing`），数据保存在 `db_data` 卷中。
API 启动时会自动等待数据库就绪并建表（含共享接收序号序列），无需手工迁移。

停止并清空数据：`docker compose down -v`。

### 本地运行（不用 Docker）

```bash
pip install -r requirements-dev.txt
export DATABASE_URL="postgresql+psycopg2://timing:timing@localhost:5432/timing"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试与冒烟

```bash
# 裁决算法单元测试（不需要数据库）
python -m pytest tests/test_adjudication.py -q

# API 集成测试（需要真实 PostgreSQL）
TEST_DATABASE_URL="postgresql+psycopg2://timing:timing@localhost:5432/timing" \
  python -m pytest tests/test_api.py -q

# 端到端冒烟（需要服务已启动），即本文档的完整示例流程
./scripts/smoke.sh http://localhost:8000
```

## 核心规则

### 赛道与过点链

- 每圈依次经过 `checkpoints + [finish]`，共 `len(checkpoints)+1` 个点位；`laps` 圈的全部期望点位
  为 `(checkpoints + [finish]) × laps`。终点即下一圈起点：第 i 圈终点读数与第 i+1 圈首点读数
  之间复用「起点→首点」的区间。
- `segments` 必须恰好 `len(checkpoints)+1` 段：起点→首点、每对相邻点之间、末点→终点，
  每段 `0 < min_seconds ≤ max_seconds`（闭区间，秒）。
- 合法过点链是期望点位序列的**前缀**：第 1 个点位读数与发枪时间之差、相邻两个采纳读数之差，
  都必须落在对应赛段区间内。链不能跳过点位续接（防止抄近道）。
- 选链目标（按优先级）：① 过点数最多；② 末点读数时间最早；③ 整链读数 ID 元组字典序最小。
- 只有完成全部 `laps × (len(checkpoints)+1)` 个点位才算 `finished`（完赛），
  此时 `elapsed_seconds = 末点读数时间 − 发枪时间`；否则为 `dnf`，`elapsed_seconds` 为 `null`。

### 读数、作废与接收序号

- 所有事件（读数、作废）共享一个单调递增的接收序号 `receive_seq`（PostgreSQL 序列），
  与上传先后一致，与读数上的比赛时间无关 —— 因此乱序补传不影响已发布版本。
- `POST /readings`：同 ID 同内容 → `200` 幂等返回原读数；同 ID 异内容 → `409`；新 ID → `201`。
- `POST /voids`：只能对**已存在**的读数追加作废事件（否则 `404`）；同一读数重复作废幂等返回 `200`。
  读数本身永不删除或修改。

### 裁决版本

- `POST /tracks/{track_id}/chips/{chip_id}/adjudications`，请求体 `{"expected_version": N}`：
  - 当前最新版本号必须等于 `expected_version`（尚无任何版本时为 `0`），否则 `409`；
  - 成功时在**同一事务**中：锁定该 (赛道, 芯片) 的版本头 → 校验预期版本 →
    获取该芯片的事务级咨询锁 → 固定接收序号高水位 `receive_seq_high` →
    取该芯片 `receive_seq ≤ 高水位` 的读数与作废事件计算裁决 →
    写入不可变版本行（含赛道快照与完整结果）→ 推进版本号。
  - **原子边界保证**：读数/作废事件在领取接收序号前同样持有该芯片的咨询锁直到提交，
    因此「锁获取顺序 = 序号领取顺序 = 提交顺序」。边界前提交的该芯片事件全部
    进入该版；边界后提交的事件，其序号必然大于旧版高水位，只能进入后续版本 ——
    不会出现「已领取序号但尚未提交的事件被旧版高水位覆盖」的错配。
  - 版本内容只取决于 `(赛道快照, receive_seq ≤ 高水位 的事件集)`，之后到达的补传/作废
    只会影响下一个版本，旧版本永远不变。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/tracks` | 创建不可变赛道 → `201`；校验失败 `422` |
| `GET` | `/tracks`、`/tracks/{id}` | 查询赛道 |
| `POST` | `/readings` | 上传读数：新 → `201`；同 ID 同内容 → `200`；同 ID 异内容 → `409` |
| `GET` | `/readings/{id}`、`/chips/{chip_id}/readings` | 查询读数（含 `voided` 标记） |
| `POST` | `/voids` | 追加作废事件：新 → `201`；已作废 → `200`；读数不存在 → `404` |
| `POST` | `/tracks/{tid}/chips/{cid}/adjudications` | 发布裁决新版本 → `201`；版本冲突 → `409` |
| `GET` | `/tracks/{tid}/chips/{cid}/adjudications` | 版本列表 |
| `GET` | `/tracks/{tid}/chips/{cid}/adjudications/{v}` | 查询指定版本（不可变） |

## 端到端示例（真实请求与响应）

以下输出取自对当前实现的真实调用（`./scripts/smoke.sh` 可复现完整流程）。
每次运行生成的 UUID 与 `created_at` 会不同。

### 1. 创建赛道

```bash
curl -X POST http://localhost:8000/tracks \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Tianzhu Trail 2-Lap",
    "gun_time": "2026-10-01T08:00:00Z",
    "checkpoints": ["CP1", "CP2"],
    "finish": "FIN",
    "laps": 2,
    "segments": [
      {"min_seconds": 300, "max_seconds": 900},
      {"min_seconds": 300, "max_seconds": 900},
      {"min_seconds": 300, "max_seconds": 900}
    ]
  }'
```

`201 Created`：

```json
{
    "id": "6ccefa90-2655-41e6-acf9-6734e34018fb",
    "name": "Tianzhu Trail 2-Lap",
    "gun_time": "2026-10-01T08:00:00Z",
    "checkpoints": [
        "CP1",
        "CP2"
    ],
    "finish": "FIN",
    "laps": 2,
    "segments": [
        {
            "min_seconds": 300,
            "max_seconds": 900
        },
        {
            "min_seconds": 300,
            "max_seconds": 900
        },
        {
            "min_seconds": 300,
            "max_seconds": 900
        }
    ],
    "created_at": "2026-09-29T21:00:40.718051Z"
}
```

校验失败（如 `min_seconds > max_seconds`、圈数为 0、时间缺少时区、段数不对）返回 `422`，例如：

```json
{
    "detail": [
        {
            "type": "value_error",
            "loc": ["body", "segments", 0],
            "msg": "Value error, min_seconds must be <= max_seconds",
            ...
        }
    ]
}
```

### 2. 乱序上传读数

CHIP-001 的两圈完整过点（08:10/08:20/08:30 第一圈，08:40/08:50/09:00 第二圈），
故意按乱序上传，另加三条干扰读数：`rd-07`（CP2 的重复合法读数）、`rd-08`（未知点位 CP9）、
`rd-09`（CP1 误读，稍后作废）：

```bash
for r in \
  '{"id":"rd-06","chip_id":"CHIP-001","point":"FIN","time":"2026-10-01T09:00:00Z"}' \
  '{"id":"rd-03","chip_id":"CHIP-001","point":"FIN","time":"2026-10-01T08:30:00Z"}' \
  '{"id":"rd-01","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:10:00Z"}' \
  '{"id":"rd-04","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:40:00Z"}' \
  '{"id":"rd-02","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:20:00Z"}' \
  '{"id":"rd-05","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:50:00Z"}' \
  '{"id":"rd-07","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:21:00Z"}' \
  '{"id":"rd-08","chip_id":"CHIP-001","point":"CP9","time":"2026-10-01T08:25:00Z"}' \
  '{"id":"rd-09","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:11:00Z"}' ; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/readings \
    -H 'Content-Type: application/json' -d "$r"
done
# 全部返回 201
```

**幂等**：同 ID 同内容重复上传返回 `200` 与原读数（注意 `receive_seq` 仍是首次接收的 `1`）：

```bash
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"id":"rd-06","chip_id":"CHIP-001","point":"FIN","time":"2026-10-01T09:00:00Z"}'
```

`200 OK`：

```json
{
    "id": "rd-06",
    "chip_id": "CHIP-001",
    "point": "FIN",
    "time": "2026-10-01T09:00:00Z",
    "receive_seq": 1,
    "voided": false,
    "created_at": "2026-09-29T21:00:40.804584Z"
}
```

**冲突**：同 ID 异内容返回 `409` 并附上已存在的读数：

```bash
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"id":"rd-01","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:12:00Z"}'
```

`409 Conflict`：

```json
{
    "detail": "reading id 'rd-01' already exists with different content",
    "existing": {
        "id": "rd-01",
        "chip_id": "CHIP-001",
        "point": "CP1",
        "time": "2026-10-01T08:10:00Z",
        "receive_seq": 3,
        "voided": false,
        "created_at": "2026-09-29T21:00:40.887090Z"
    }
}
```

### 3. 发布裁决 v1

```bash
curl -X POST http://localhost:8000/tracks/6ccefa90-2655-41e6-acf9-6734e34018fb/chips/CHIP-001/adjudications \
  -H 'Content-Type: application/json' -d '{"expected_version": 0}'
```

`201 Created`（`rd-07`/`rd-09` 虽也是合法候选，但整链 ID 字典序输给了 `rd-01…rd-06` 链；
`rd-08` 点位不在赛道上，直接被排除）：

```json
{
    "id": "34b3f7f3-af44-4ce4-8d21-46a83abd1700",
    "track_id": "6ccefa90-2655-41e6-acf9-6734e34018fb",
    "chip_id": "CHIP-001",
    "version": 1,
    "receive_seq_high": 9,
    "status": "finished",
    "laps_completed": 2,
    "points_passed": 6,
    "total_points": 6,
    "elapsed_seconds": 3600.0,
    "adopted_readings": [
        {"id": "rd-01", "point": "CP1", "time": "2026-10-01T08:10:00Z", "position": 1, "lap": 1},
        {"id": "rd-02", "point": "CP2", "time": "2026-10-01T08:20:00Z", "position": 2, "lap": 1},
        {"id": "rd-03", "point": "FIN", "time": "2026-10-01T08:30:00Z", "position": 3, "lap": 1},
        {"id": "rd-04", "point": "CP1", "time": "2026-10-01T08:40:00Z", "position": 4, "lap": 2},
        {"id": "rd-05", "point": "CP2", "time": "2026-10-01T08:50:00Z", "position": 5, "lap": 2},
        {"id": "rd-06", "point": "FIN", "time": "2026-10-01T09:00:00Z", "position": 6, "lap": 2}
    ],
    "excluded_readings": [
        {"id": "rd-07", "point": "CP2", "time": "2026-10-01T08:21:00Z"},
        {"id": "rd-08", "point": "CP9", "time": "2026-10-01T08:25:00Z"},
        {"id": "rd-09", "point": "CP1", "time": "2026-10-01T08:11:00Z"}
    ],
    "voided_readings": [],
    "track_snapshot": {
        "id": "6ccefa90-2655-41e6-acf9-6734e34018fb",
        "name": "Tianzhu Trail 2-Lap",
        "gun_time": "2026-10-01T08:00:00Z",
        "checkpoints": ["CP1", "CP2"],
        "finish": "FIN",
        "laps": 2,
        "segments": [
            {"min_seconds": 300, "max_seconds": 900},
            {"min_seconds": 300, "max_seconds": 900},
            {"min_seconds": 300, "max_seconds": 900}
        ]
    },
    "created_at": "2026-09-29T21:00:41.282071Z"
}
```

### 4. 作废误读 + 乱序补传（不影响 v1）

```bash
# 作废误读 rd-09 -> 201
curl -X POST http://localhost:8000/voids -H 'Content-Type: application/json' \
  -d '{"reading_id":"rd-09","reason":"misread: duplicate of rd-01"}'
```

```json
{
    "id": "1612d228-50dd-4d6e-95db-776d78c188d7",
    "reading_id": "rd-09",
    "reason": "misread: duplicate of rd-01",
    "receive_seq": 10,
    "created_at": "2026-09-29T21:00:41.339137Z"
}
```

重复作废同一读数幂等返回 `200` 与同一作废事件；对不存在的读数作废返回 `404`。

```bash
# 迟到的补传：rd-00（CP1 08:05，合法的第 1 点候选）与 rd-10（CP2 08:22）
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"id":"rd-00","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:05:00Z"}'
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"id":"rd-10","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:22:00Z"}'
```

### 5. 过期版本号 → 409；用预期版本号重裁决 → v2

```bash
curl -X POST http://localhost:8000/tracks/6ccefa90-2655-41e6-acf9-6734e34018fb/chips/CHIP-001/adjudications \
  -H 'Content-Type: application/json' -d '{"expected_version": 0}'
```

`409 Conflict`：

```json
{
    "detail": "version conflict: expected_version=0 but current_version=1",
    "current_version": 1
}
```

以正确的预期版本号重裁决：

```bash
curl -X POST http://localhost:8000/tracks/6ccefa90-2655-41e6-acf9-6734e34018fb/chips/CHIP-001/adjudications \
  -H 'Content-Type: application/json' -d '{"expected_version": 1}'
```

`201 Created`。补传的 `rd-00` 使整链 ID 字典序更小（`("rd-00",…) < ("rd-01",…)`），
被 v2 采纳；`rd-09` 已作废，移入 `voided_readings`；`rd-01`、`rd-07`、`rd-08`、`rd-10` 进入排除列表：

```json
{
    "id": "9cbde492-b61d-40b6-8a4e-30d642e3d0a7",
    "track_id": "6ccefa90-2655-41e6-acf9-6734e34018fb",
    "chip_id": "CHIP-001",
    "version": 2,
    "receive_seq_high": 12,
    "status": "finished",
    "laps_completed": 2,
    "points_passed": 6,
    "total_points": 6,
    "elapsed_seconds": 3600.0,
    "adopted_readings": [
        {"id": "rd-00", "point": "CP1", "time": "2026-10-01T08:05:00Z", "position": 1, "lap": 1},
        {"id": "rd-02", "point": "CP2", "time": "2026-10-01T08:20:00Z", "position": 2, "lap": 1},
        {"id": "rd-03", "point": "FIN", "time": "2026-10-01T08:30:00Z", "position": 3, "lap": 1},
        {"id": "rd-04", "point": "CP1", "time": "2026-10-01T08:40:00Z", "position": 4, "lap": 2},
        {"id": "rd-05", "point": "CP2", "time": "2026-10-01T08:50:00Z", "position": 5, "lap": 2},
        {"id": "rd-06", "point": "FIN", "time": "2026-10-01T09:00:00Z", "position": 6, "lap": 2}
    ],
    "excluded_readings": [
        {"id": "rd-01", "point": "CP1", "time": "2026-10-01T08:10:00Z"},
        {"id": "rd-07", "point": "CP2", "time": "2026-10-01T08:21:00Z"},
        {"id": "rd-08", "point": "CP9", "time": "2026-10-01T08:25:00Z"},
        {"id": "rd-10", "point": "CP2", "time": "2026-10-01T08:22:00Z"}
    ],
    "voided_readings": [
        {
            "id": "rd-09",
            "point": "CP1",
            "time": "2026-10-01T08:11:00Z",
            "void_event_id": "1612d228-50dd-4d6e-95db-776d78c188d7",
            "reason": "misread: duplicate of rd-01"
        }
    ],
    "track_snapshot": { "...": "与 v1 相同（赛道不可变）" },
    "created_at": "2026-09-29T21:00:41.544291Z"
}
```

### 6. 旧版本不可变

```bash
curl http://localhost:8000/tracks/6ccefa90-2655-41e6-acf9-6734e34018fb/chips/CHIP-001/adjudications/1
```

`200 OK`：v1 仍是发布时的内容 —— `receive_seq_high` 仍为 `9`，采纳链仍以 `rd-01` 开头，
`voided_readings` 仍为空（补传与作废全部不影响它）：

```json
{
    "id": "34b3f7f3-af44-4ce4-8d21-46a83abd1700",
    "version": 1,
    "receive_seq_high": 9,
    "status": "finished",
    "elapsed_seconds": 3600.0,
    "adopted_readings": [
        {"id": "rd-01", "point": "CP1", "time": "2026-10-01T08:10:00Z", "position": 1, "lap": 1},
        "...": "rd-02 … rd-06 与发布时完全一致"
    ],
    "excluded_readings": [
        {"id": "rd-07", "point": "CP2", "time": "2026-10-01T08:21:00Z"},
        {"id": "rd-08", "point": "CP9", "time": "2026-10-01T08:25:00Z"},
        {"id": "rd-09", "point": "CP1", "time": "2026-10-01T08:11:00Z"}
    ],
    "voided_readings": []
}
```

版本列表 `GET /tracks/{tid}/chips/CHIP-001/adjudications` 返回 `[v1, v2]`。

### 7. 未完赛（DNF）示例

CHIP-002 只有 CP1、CP2 两个有效过点，缺第一圈终点读数，链在第 2 点断开：

```bash
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"id":"rd-20","chip_id":"CHIP-002","point":"CP1","time":"2026-10-01T08:10:00Z"}'
curl -X POST http://localhost:8000/readings -H 'Content-Type: application/json' \
  -d '{"id":"rd-21","chip_id":"CHIP-002","point":"CP2","time":"2026-10-01T08:20:00Z"}'
curl -X POST http://localhost:8000/tracks/6ccefa90-2655-41e6-acf9-6734e34018fb/chips/CHIP-002/adjudications \
  -H 'Content-Type: application/json' -d '{"expected_version": 0}'
```

`201 Created`：

```json
{
    "id": "1e5a364b-a148-4742-8105-04e0e565dc32",
    "track_id": "6ccefa90-2655-41e6-acf9-6734e34018fb",
    "chip_id": "CHIP-002",
    "version": 1,
    "receive_seq_high": 14,
    "status": "dnf",
    "laps_completed": 0,
    "points_passed": 2,
    "total_points": 6,
    "elapsed_seconds": null,
    "adopted_readings": [
        {"id": "rd-20", "point": "CP1", "time": "2026-10-01T08:10:00Z", "position": 1, "lap": 1},
        {"id": "rd-21", "point": "CP2", "time": "2026-10-01T08:20:00Z", "position": 2, "lap": 1}
    ],
    "excluded_readings": [],
    "voided_readings": [],
    "track_snapshot": { "...": "同上" },
    "created_at": "2026-09-29T21:00:41.722894Z"
}
```

## 实现要点

- **裁决算法**（`app/adjudication.py`，纯函数）：期望点位序列上的动态规划。
  对每个期望位置、对该位置的每条候选读数，保留「以该读数结尾、ID 元组字典序最小」的链；
  因为最终比较只看（链长, 末点时间, 整链 ID 元组），且向后扩展只取决于末条读数，
  所以每个候选读数只保留最优前缀即完备。读数按 `(时间, ID)` 排序后逐位置扩展，
  第一个无法续接的位置即链的终点 —— 天然保证「先最大化过点数」。
- **接收序号高水位**：读数与作废事件共用序列 `receive_seq_seq`（`CACHE 1`）。
  序列取值是非事务性的 —— `nextval` 一经调用即对其他会话可见，而插入的行要等
  提交后才可见 —— 因此仅靠「发布事务读序列当前值」无法界定一致边界：若某事件
  已取号但尚未提交，发布事务会把高水位定在它之上却读不到它，待其提交后，
  旧版高水位便覆盖了一条从未参与裁决的事件。为此，事件入库
  （`POST /readings`、`POST /voids` 的实际插入路径）在取号前先获取该芯片的
  事务级咨询锁 `pg_advisory_xact_lock(hashtextextended(chip_id, 0))` 并持有到提交；
  发布事务在固定高水位前也获取同一把锁。锁获取顺序 = 取号顺序 = 提交顺序，
  赛道快照、高水位与裁决输入由此对应同一原子边界。
- **并发控制**：每个 (赛道, 芯片) 有一行版本头，发布时 `SELECT … FOR UPDATE` 串行化，
  并有 `(track_id, chip_id, version)` 唯一约束兜底；每芯片一把咨询锁只串行化
  该芯片的「事件入库 vs 发布定界」，不同芯片之间互不阻塞；读数主键冲突时按
  「同内容幂等 / 异内容 409」重判定，并发上传安全。
- **不可变性**：赛道无更新接口；读数不可改、只能追加作废；裁决版本行只插不改，
  内容含赛道快照与完整结果 JSON，旧版本可永久复查。
