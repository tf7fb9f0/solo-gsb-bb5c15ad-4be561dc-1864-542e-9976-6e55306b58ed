# 板材直切排样 API（Panel Guillotine Nesting API）

面向小型加工坊的矩形零件母板排样服务。输入母板尺寸、锯缝宽度、母板数量上限和
**带数量与可旋转标记**的矩形零件，输出只由**贯穿当前余料区域的直切**
（guillotine cut）组成、可直接上机执行的切割方案：每件零件落在第几号母板、
坐标、朝向，以及按顺序编号的切割线和最终余料矩形。

- 语言/框架：Python 3.12 · FastAPI · Pydantic v2
- 数据库：SQLite（单文件，WAL 模式，启动自动建库）
- 保证：零件不重叠、不越界；锯缝实际占位；同输入稳定（确定性算法 + 幂等存储）；
  排不下的零件以明确原因列出。

---

## 1. 一条命令启动（Docker Compose）

依赖安装在镜像构建时完成；容器启动时自动初始化 SQLite 数据库并运行服务。

```bash
docker compose up --build -d
```

这一条命令完成三件事：**安装依赖 → 初始化数据库 → 启动 API**。

- 访问地址：<http://localhost:8000>
- 健康检查：<http://localhost:8000/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>

> 端口在 `docker-compose.yml` 中映射为宿主机 `8000:8000`，可按需修改。
> 数据库保存在命名卷 `nesting-data`（容器内 `/app/data/nesting.db`），
> 重建容器数据不丢。

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止
docker compose down -v         # 停止并删除数据库卷
```

### 不用 Docker 的本地启动

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export NESTING_DB_PATH=./data/nesting.db   # 可选，默认即此路径
python -m app.init_db                       # 初始化数据库（或交给启动钩子自动完成）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

或直接执行容器同款入口脚本（初始化 + 启动一步到位）：

```bash
pip install -r requirements.txt
./entrypoint.sh
```

---

## 2. 接口

### `POST /api/v1/plans` — 创建/获取排样方案

相同请求是**幂等**的：`plan_id` 由请求内容的 SHA256 派生，重复提交返回同一份
已存方案（连创建时间都不变），因此“相同输入永远得到相同结果”。

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `board_width` | number, >0 | 母板宽（mm） |
| `board_height` | number, >0 | 母板高（mm） |
| `kerf_width` | number, ≥0，小于母板宽高 | 锯缝宽度（mm），默认 0 |
| `max_boards` | integer, ≥1 | 母板数量上限 |
| `parts` | array，≥1 | 零件清单 |
| `parts[].id` | string | 零件编号，同次请求内不可重复 |
| `parts[].width` / `height` | number, >0 | 零件宽 / 高（mm） |
| `parts[].quantity` | integer, ≥1 | 数量 |
| `parts[].rotatable` | boolean | 是否允许旋转 90° |

示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "board_width": 244,
    "board_height": 122,
    "kerf_width": 3,
    "max_boards": 3,
    "parts": [
      {"id": "door",  "width": 120, "height": 60, "quantity": 2, "rotatable": true},
      {"id": "shelf", "width": 60,  "height": 40, "quantity": 3, "rotatable": true}
    ]
  }'
```

响应关键字段：

```jsonc
{
  "plan_id": "a6df23c6-…",
  "status": "complete",            // complete=全部排入；partial=有未排零件
  "created_at": "2026-09-19T17:41:42Z",
  "summary": {
    "panels_used": 2,              // 实际使用母板数（≤ max_boards）
    "max_boards": 3,
    "placed_parts": 5,
    "unplaced_parts": 0,
    "utilization": 0.362806,       // 零件总面积 / 已用母板总面积
    "board_width": 244.0, "board_height": 122.0, "kerf_width": 3.0
  },
  "panels": [
    {
      "panel_no": 1,               // 母板编号，从 1 开始
      "width": 244.0, "height": 122.0,
      "placements": [              // 每件零件一条
        {"part_id": "door", "instance": 1, "rotated": false,
         "x": 0.0, "y": 0.0, "width": 120.0, "height": 60.0}
      ],
      "cuts": [                    // 按 step 顺序执行
        {
          "step": 1,
          "axis": "V",             // V=竖切(沿y贯穿当前区域全高)，H=横切(沿x贯穿全宽)
          "x": 120.0, "y": 0.0,
          "length": 122.0,         // 切割线长度（锯缝宽度方向不重复计长）
          "region": {"x": 0.0, "y": 0.0, "width": 244.0, "height": 122.0}
        }
      ],
      "remnants": [                // 最终余料矩形（锯缝不在余料内）
        {"x": 123.0, "y": 0.0, "width": 121.0, "height": 122.0}
      ]
    }
  ],
  "unplaced": []                   // 未排零件，见下
}
```

### 未排零件说明

排不下时 `status` 为 `"partial"`，`unplaced` 数组逐项列出：

```jsonc
"unplaced": [
  {"part_id": "A", "quantity": 1, "reason": "board_limit"},
  {"part_id": "B", "quantity": 4, "reason": "oversize"}
]
```

| reason | 含义 |
|---|---|
| `oversize` | 该零件无论是否旋转（受 `rotatable` 限制）都无法放入任何一块空母板（含锯缝占位后仍超限） |
| `board_limit` | 零件本身放得下，但现有母板的余料都排不进，且已达到 `max_boards` 上限 |

### `GET /api/v1/plans/{plan_id}` — 查询方案

返回与创建时完全一致的方案；不存在返回 `404`。

```bash
curl -s http://localhost:8000/api/v1/plans/a6df23c6-b019-79c7-2652-933b0d34d5e5
```

### `GET /api/v1/plans?limit=50&offset=0` — 方案列表

按创建时间倒序返回摘要（`plan_id / status / created_at / panels_used /
placed_parts / unplaced_parts`）及 `total`。

### `GET /health`

返回 `{"status":"ok"}`，供容器健康检查与负载均衡探活。

---

## 3. 切割方案的几何语义

坐标系：母板左上角为原点 `(0,0)`，**x 向右、y 向下**，单位 mm。

- 零件 `placements[].x/y` 为其左上角；`rotated=true` 表示相对原始宽高旋转了 90°，
  此时返回的 `width/height` 是旋转后实际占位（`width` 仍对应 x 方向）。
- 每件零件放置在一个矩形余料区域的左下角。放置后做 1~2 刀：
  1. 第一刀沿“富余较长”的方向**贯穿当前区域**（`sx ≥ sy` 先竖切，否则先横切，
     平局先竖切）；
  2. 第二刀（若需要）在零件所在一侧贯穿该侧，把零件与余料分开。
- 每块母板的 `cuts[]` 已按 `step` 排好可执行顺序：**先执行的刀会把后续刀所在的
  区域完整切出来**，即每一刀都贯穿它 `region` 字段所指的当前余料矩形。
  - `axis:"V"`：竖刀在 `x` 处，沿 y 方向，长度 = `region.height`；
  - `axis:"H"`：横刀在 `y` 处，沿 x 方向，长度 = `region.width`；
  - 锯缝在切刀线的“右侧/下侧”占位，宽 `kerf_width`；相邻零件/余料坐标之间
    会自动空出这一条缝。
- `remnants[]` 是所有切割完成后剩下的可用矩形（不含锯缝、不含零件）。

排样引擎在生成方案时内置严格自检：逐刀模拟贯穿切、校验切割后矩形集合恰为
“零件 ∪ 余料”、面积平衡（零件 + 余料 + 锯缝 = 母板面积）、零件不重叠不越界、
数量守恒。任何自检失败都会直接报错而不会返回错误方案。

### 确定性

- 零件实例按（最长边降序、面积降序、id、输入序号）固定排序；
- 所有候选位置用固定元组打分：优先用已有母板，其次浪费面积最小，再以
  原始朝向优先、母板号、x、y 裁决平局；
- 因此无论零件在请求中的先后顺序如何，同一输入（含数量、旋转标记）总是得到
  同一份方案，且结果以内容哈希持久化、天然幂等。

---

## 4. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（创建/查询/列表/健康检查）+ 启动建库
│   ├── models.py        # 请求/响应 Pydantic 模型与参数校验
│   ├── nesting.py       # 直切排样核心算法 + 几何自检 + 响应装配
│   ├── services.py      # 业务编排、幂等落库
│   ├── database.py      # SQLite 连接、建表、plan_id 摘要、CRUD
│   └── init_db.py       # python -m app.init_db 建库入口
├── tests/               # unittest：核心算法/随机不变量/持久化幂等/API 端到端
├── entrypoint.sh        # 容器启动：建库 + uvicorn（一条命令）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 5. 运行测试

```bash
pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
```

测试覆盖：恰好放满、锯缝切分、旋转启用/禁用、母板上限、超大件、编号连续、
零锯缝铺满、**300 组随机算例的贯穿切模拟与面积/重叠/数量不变量**、
结果稳定性（含输入乱序）、SQLite 幂等存取，以及基于 TestClient 的 HTTP 端到端。
