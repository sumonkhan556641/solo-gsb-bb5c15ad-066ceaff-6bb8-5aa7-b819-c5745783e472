# 食品换线放行服务（共用产线过敏原裁决）

面向**共用产线**的食品换线放行：按产线顺序登记生产批次、所用原料批号及其过敏原、
成品声明过敏原、批次开工前清洁核验确认清除的过敏原；服务计算每批**实际可能含有的过敏原**、
**未声明项**，给出**放行 / 扣留结论及原因**，并支持原料批号与清洁核验更正后的**顺序重算**
与**可查询的历史裁决版本**。

此外支持**出货核验单**：一张核验单关联多个已登记生产批次，逐批冻结创建时的裁决版本与结论；
草稿可重新核验刷新快照，确认时原子校验“快照版本仍为当前版本且全部 RELEASE”；
已确认单据所含批次一旦因更正重算产生新裁决版本（即使结论仍为 RELEASE），立即转**待复核**，
重新核验后才能再次确认；历次确认快照只追加保留，HOLD 批次始终不能确认。

- 技术栈：Python 3.11 + FastAPI + SQLite（标准库 `sqlite3`，WAL 模式）
- 数据落盘：单个 SQLite 文件，Docker 卷持久化
- 一键启动：`docker compose up`（首次启动自动建表、写入过敏原字典与演示原料批号）

---

## 1. 业务规则

同一产线 `line_id` 上，批次按登记顺序获得递增序号 `seq`，对第 N 批：

```
带入  = 上一批（第 N-1 批）实际可能含有的过敏原
残留  = 带入 − 本次清洁核验“确认清除”的过敏原     # 只清除被确认覆盖的项目
本批  = 本批所用全部原料批号过敏原的并集
实际  = 残留 ∪ 本批
未声明 = 实际 − 成品声明过敏原
结论  = 未声明非空 → HOLD（扣留）；否则 RELEASE（放行）
```

要点：

- **清洁只清除被确认覆盖的过敏原**：清洁清单中未列出的残留过敏原继续留在产线。
- **上一批即使被扣留（HOLD），其实际过敏原仍继续向下传递**。
- 清洁发生在本批开工前，只能清除“上一批留下的”残留；本批原料再次引入的过敏原仍计入实际。
- **更正原料批号或某批清洁/声明/用料后**：从受影响的最早批次开始，按产线顺序重算该批及
  之后的全部批次（原料批号更正会覆盖所有引用它的产线）；每次重算追加一个**只增不改的历史
  裁决版本**，可随时查询。
- 更正接口为**全量替换**语义（传入的数组代表更正后的完整集合，不传的字段保持不变）。

拒绝规则（失败时整个请求回滚，**不留下部分改动**）：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 批次号重复 | 409 | `DUPLICATE_BATCH` |
| 原料批号重复登记 | 409 | `DUPLICATE_MATERIAL_LOT` |
| 批次引用了未登记的原料批号 | 400 | `UNKNOWN_MATERIAL_LOT` |
| 过敏原代码不在受控字典内 | 400 | `INVALID_ALLERGEN` |
| 字段为空/类型错误等负载问题 | 400/422 | `INVALID_PAYLOAD` / FastAPI 校验 |
| 更正目标批次/原料批号不存在 | 404 | `CORRECTION_TARGET_NOT_FOUND` |
| 查询不存在的批次/原料批号 | 404 | `BATCH_NOT_FOUND` / `MATERIAL_LOT_NOT_FOUND` |

过敏原受控字典采用欧盟 14 类法定过敏原代码（仅接受这些代码）：

`gluten`（含麸质谷物）、`crustaceans`（甲壳类）、`egg`（蛋）、`fish`（鱼）、
`peanut`（花生）、`soy`（大豆）、`milk`（乳）、`tree_nuts`（坚果）、`celery`（芹菜）、
`mustard`（芥末）、`sesame`（芝麻）、`sulphites`（亚硫酸盐）、`lupin`（羽扇豆）、
`molluscs`（软体动物）。完整清单可通过 `GET /api/allergens` 查询。

---

## 2. 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
# 后台运行：docker compose up -d --build
# 停止并保留数据：docker compose down
# 停止并删除数据卷（彻底重置）：docker compose down -v
```

容器首次启动自动完成：建表 → 写入 14 项过敏原字典 → 写入 4 个演示原料批号与示例产线 `L1`。
数据保存在命名卷 `changeover-data`（容器内 `/data/changeover.db`）。

## 3. 本地直接运行（不使用 Docker）

```bash
pip install -r requirements.txt

# 可选：初始化空库时写入演示原料批号与产线 L1
export SEED_DEMO_DATA=1

python -m app
```

本地默认数据库文件为仓库内 `data/changeover.db`（自动创建目录）。

## 4. 运行测试与示例

```bash
pip install pytest httpx
python -m pytest -q            # 35 个用例：裁决、传递、更正重算、历史、原子性、产线隔离、出货核验单

# 服务启动后，运行端到端调用示例（标准库 urllib，无需额外依赖）
python examples/run_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/run_demo.py   # 显式指定地址

# 出货核验单端到端示例（创建/拒绝/HOLD 拦截/确认/更正转待复核/重新核验/二次确认）
python examples/verification_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/verification_demo.py
```

---

## 5. 配置（环境变量）

| 变量 | 默认值（本地） | 容器默认 | 说明 |
| --- | --- | --- | --- |
| `HOST` | `0.0.0.0` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | `8080` | 监听端口 |
| `DB_PATH` | `./data/changeover.db` | `/data/changeover.db` | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `0`（关） | `1`（开） | 仅在**全新空库**初始化时写入演示原料批号/产线；对已有库幂等、不重复写入 |

## 6. 服务地址与交互式文档

启动后默认地址端口：**http://127.0.0.1:8080**

- 健康检查：`GET /health`
- Swagger 交互文档：http://127.0.0.1:8080/docs
- OpenAPI 描述：http://127.0.0.1:8080/openapi.json

---

## 7. HTTP API 一览

### 参考数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/allergens` | 过敏原受控字典 |
| POST | `/api/material-lots` | 登记原料批号（lot_no、material、allergens） |
| GET | `/api/material-lots` | 列出全部原料批号 |
| GET | `/api/material-lots/{lot_no}` | 查询单个原料批号 |
| PUT | `/api/material-lots/{lot_no}` | **更正原料批号**（名称/过敏原），重算受影响批次及后续批次 |
| GET | `/api/lines` | 产线列表及各线批次数量 |

### 批次与裁决

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | **按产线顺序登记批次**，立即返回裁决（seq 自动取该线下一号） |
| GET | `/api/batches?line_id=L1` | 列出批次（可按产线过滤） |
| GET | `/api/batches/{batch_no}` | 查询批次及当前裁决快照 |
| PUT | `/api/batches/{batch_no}` | **更正批次**（用料/声明/清洁，任一可选），重算本批及后续批次 |
| PUT | `/api/batches/{batch_no}/cleaning` | 专用：仅更正开工前清洁核验确认清除的过敏原 |
| GET | `/api/batches/{batch_no}/history` | 查询该批**历史裁决版本**（只追加，含触发原因与时间） |

### 裁决响应字段

```jsonc
{
  "batch_no": "B2026003", "line_id": "L1", "seq": 3,
  "material_lots": [],
  "declared_allergens": [],
  "cleaned_allergens": [],
  "adjudication": {
    "version": 1,
    "actual_allergens": ["peanut"],      // 实际可能含有的过敏原
    "undeclared_allergens": ["peanut"],  // 未声明项（非空即扣留）
    "disposition": "HOLD",               // RELEASE / HOLD
    "reasons": [ /* 逐条原因，含过敏原与来源说明 */ ],
    "sources": { "peanut": ["上一批残留在共用产线、本次清洁未确认清除"] },
    "decided_at": "2026-09-30T00:00:00+00:00"
  }
}
```

更正接口额外返回 `recomputed`：本次按序重算的批次裁决摘要列表；
原料批号更正会列出各产线受影响批次。
更正若重算了核验单所含批次，响应还附带 `affected_verifications`（含各核验单更正后的状态）。

### 出货核验单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/verifications` | 创建核验单（草稿）：唯一单号 + 非空、无重复的已登记批次号列表，逐批保存当前裁决版本与结论 |
| GET | `/api/verifications?status=DRAFT` | 列出核验单，可按 `DRAFT` / `PENDING_REVIEW` / `CONFIRMED` 过滤 |
| GET | `/api/verifications/{verification_no}` | 核验单详情：状态、逐批记录（快照版本/当前版本/变化原因）、历次确认快照 |
| POST | `/api/verifications/{verification_no}/reverify` | **重新核验**：刷新逐批快照并回到草稿（仅 DRAFT / PENDING_REVIEW 可调用） |
| POST | `/api/verifications/{verification_no}/confirm` | **确认**：原子校验逐批快照仍为当前版本且全部 RELEASE |

**状态机**：

```
DRAFT ──confirm 成功──▶ CONFIRMED
  ▲                        │ 所含批次因更正重算产生新裁决版本（即使仍为 RELEASE）
  └── reverify ── PENDING_REVIEW ◀┘
```

- 创建即 `DRAFT`，逐批冻结当时的裁决 `version` 与 `disposition`（快照）。
- 确认是一次**原子校验**：每批快照版本必须等于当前裁决版本，且结论必须为 `RELEASE`。
  任一不符返回 **422 `VERIFICATION_NOT_CONFIRMABLE`**，`error.details[]` 列出**全部**不符批次：
  - `STALE_SNAPSHOT`：快照版本已落后（含 `snapshot_version` / `current_version` / `change_reasons`）；
  - `BATCH_ON_HOLD`：裁决为 HOLD，**HOLD 批次始终不能确认**（含未声明过敏原清单）。
  校验失败不写入任何确认记录，单据保持未确认。
- `CONFIRMED` 单据所含批次一旦产生新裁决版本，立即自动转 `PENDING_REVIEW`；
  待复核单据必须先 `reverify`（快照刷新、回到 `DRAFT`）才能再次确认。
- 每次成功确认都向**只追加**的确认历史写入一份逐批快照不可变副本；重新确认不覆盖历史。
- 同一批次可同时被多张核验单关联；草稿/待复核单据不会被更正联动转态（其快照本就待刷新）。

核验单创建请求体：

```json
{ "verification_no": "SH-20260930-01", "batch_nos": ["B001", "B003"] }
```

详情中每批 `batches[]` 记录：

```jsonc
{
  "position": 1, "batch_no": "B001", "line_id": "L1", "seq": 1,
  "snapshot": { "version": 1, "disposition": "RELEASE", /* actual/undeclared/reasons */ },
  "current":  { "version": 3, "disposition": "RELEASE", /* ...完整当前裁决... */ },
  "changed": true,                                   // 当前版本 != 快照版本
  "change_reasons": [                                // 快照版本之后每个裁决版本一条
    { "code": "ADJUDICATION_VERSION_CHANGED",
      "from_version": 1, "to_version": 2, "trigger": "CORRECT_BATCH",
      "message": "裁决版本 v2（批次更正重算）晚于快照版本 v1，结论 RELEASE",
      "decided_at": "2026-09-30T00:00:00+00:00" }
  ]
}
```

`confirmations[]` 为历次确认（`confirmation_id`、`confirmed_at` 与当时逐批版本/结论快照）。

核验单相关拒绝码：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 核验单号重复 | 409 | `DUPLICATE_VERIFICATION` |
| 列表含未登记批次 | 400 | `UNKNOWN_BATCH` |
| 列表内批次号重复 | 400 | `DUPLICATE_BATCH_IN_VERIFICATION` |
| 单号为空 / 列表非数组 | 400 | `INVALID_PAYLOAD`（空列表为 422 模型校验） |
| 核验单不存在 | 404 | `VERIFICATION_NOT_FOUND` |
| 已确认单据再次确认或重新核验 | 409 | `VERIFICATION_ALREADY_CONFIRMED` |
| 确认时存在不符批次 | 422 | `VERIFICATION_NOT_CONFIRMABLE`（`details` 逐批说明） |

---

## 8. curl 调用示例

```bash
# 1) 登记原料批号
curl -s -X POST http://127.0.0.1:8080/api/material-lots \
  -H 'Content-Type: application/json' \
  -d '{"lot_no":"RM-MILK-002","material":"全脂奶粉","allergens":["milk"]}'

# 2) 登记第 1 批：牛奶巧克力，声明 milk —— 放行
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B001","line_id":"L1","material_lots":["RM-MILK-002"],
       "declared_allergens":["milk"],"cleaned_allergens":[]}'

# 3) 登记第 2 批：使用含花生的原料、只清洁 milk、只声明 milk —— peanut 未声明，扣留
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B002","line_id":"L1","material_lots":["RM-NUT-001"],
       "declared_allergens":["milk"],"cleaned_allergens":["milk"]}'

# 4) 第 3 批无过敏原原料且未清洁 —— 继承上批（被扣留批）残留 peanut，扣留
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B003","line_id":"L1","material_lots":[],
       "declared_allergens":[],"cleaned_allergens":[]}'

# 5) 更正 B003 开工前清洁核验：确认清除 peanut —— B003 及其后续批次自动重算
curl -s -X PUT http://127.0.0.1:8080/api/batches/B003/cleaning \
  -H 'Content-Type: application/json' \
  -d '{"cleaned_allergens":["peanut"]}'

# 6) 更正原料批号过敏原（例如复检发现还含 sesame）—— 所有引用批次按线重算
curl -s -X PUT http://127.0.0.1:8080/api/material-lots/RM-NUT-001 \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["peanut","sesame"]}'

# 7) 查询历史裁决版本
curl -s http://127.0.0.1:8080/api/batches/B003/history

# 8) 创建出货核验单（草稿）：关联两个已登记批次，逐批冻结裁决版本与结论
curl -s -X POST http://127.0.0.1:8080/api/verifications \
  -H 'Content-Type: application/json' \
  -d '{"verification_no":"SH-001","batch_nos":["B001","B003"]}'

# 9) 确认（原子校验：快照仍当前版本且全部 RELEASE；否则 422 返回不符批次明细）
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/confirm

# 10) 已确认单据所含批次被更正重算 -> 自动转 PENDING_REVIEW；重新核验刷新快照
curl -s -X POST http://127.0.0.1:8080/api/verifications/SH-001/reverify

# 11) 查询核验单（状态、逐批快照/当前版本/变化原因、历次确认快照）
curl -s http://127.0.0.1:8080/api/verifications/SH-001
curl -s 'http://127.0.0.1:8080/api/verifications?status=PENDING_REVIEW'
```

更多可运行示例见 [`examples/run_demo.py`](examples/run_demo.py)（包含完整的
登记 → 扣留传递 → 清洁更正重算 → 历史查询 → 各类拒绝场景）；
出货核验单全流程示例见 [`examples/verification_demo.py`](examples/verification_demo.py)
（创建 → HOLD 拦截 → 确认 → 更正转待复核 → 重新核验 → 二次确认与历史快照）。
示例脚本可重复调用其中的“登记原料批号”接口（重复时自动跳过）；批次号固定，
重复运行前如需重置可 `docker compose down -v` 后重新启动，或删除本地 `data/` 目录。

---

## 9. 项目结构

```
.
├── app/
│   ├── config.py        # 环境变量配置（DB_PATH / HOST / PORT / SEED_DEMO_DATA）
│   ├── allergens.py     # 14 项过敏原受控字典
│   ├── errors.py        # 业务错误（code + HTTP 状态码）
│   ├── db.py            # SQLite 连接、建表、事务、初始化/种子
│   ├── service.py       # 核心规则：传递/清洁/裁决、更正重算、历史版本、出货核验单
│   ├── schemas.py       # 请求模型
│   ├── main.py          # FastAPI 路由
│   └── __main__.py      # python -m app 入口
├── examples/run_demo.py          # 裁决/更正/历史端到端调用示例
├── examples/verification_demo.py # 出货核验单端到端调用示例
├── tests/test_service.py         # pytest 测试（裁决/更正/历史，20 项）
├── tests/test_verification.py    # pytest 测试（出货核验单，15 项）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 10. 数据库表（SQLite）

- `allergen`：过敏原字典
- `material_lot`：原料批号 → 名称、过敏原集合
- `line` / `batch` / `batch_material`：产线、批次（含 `line_id+seq` 唯一顺序）、批次用料关联
- `adjudication`：每批当前裁决快照
- `adjudication_version`：只追加的历史裁决版本（`REGISTER` / `CORRECT_BATCH` / `CORRECT_LOT`）
- `verification`：出货核验单（`DRAFT` / `PENDING_REVIEW` / `CONFIRMED`）
- `verification_batch`：核验单逐批快照（创建/重新核验时的裁决版本与结论，按提交顺序保序）
- `verification_confirmation` / `verification_confirmation_batch`：只追加的历次确认及其逐批快照副本

所有写操作在单事务（`BEGIN IMMEDIATE`）内完成并进程内串行化，配合外键约束与 WAL，
保证更正与登记的原子性：校验失败即整体回滚，不会产生半成品批次或半成品重算版本。
核验单创建（未知/重复批次在写入前整体校验）、重新核验与确认（校验与确认记录写入同一事务，
失败不留下确认记录）遵循同样的原子性保证；新表以 `CREATE TABLE IF NOT EXISTS` 幂等加入，
旧数据库启动时自动升级，既有批次裁决、更正与历史查询行为保持不变。
