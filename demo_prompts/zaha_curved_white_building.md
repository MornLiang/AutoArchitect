# 需求文档：流线型曲面白色建筑

## 1. 设计意图（来自参考图与用户描述）

参考图为扎哈·哈迪德（Zaha Hadid）所设计的阿利耶夫文化中心（Heydar Aliyev
Center, Baku）。用户在文字描述中明确了三个核心特征：

- **流线型（streamlined）**：建筑整体没有明显的直角折线，由一条连续的曲线
  形态从地面"生长"起来，再回落到地面。
- **曲面（curved surface）**：屋面与外墙是同一张连续曲面，模糊了"墙"与
  "屋顶"的边界。
- **白色外观（white exterior）**：外饰面统一为亚光白色，质感接近 GFRC
  （玻璃纤维增强混凝土）或穿孔铝板。

这是一栋作为文化／展览综合体使用的公共建筑，强调标志性与雕塑感。

## 2. 项目能力边界（来自 `ifc_agent/text2ifc/schemas.py`）

本项目的 `SpatialGraph` → `BuildingGraph` → IFC 管线有以下硬性约束：

| 维度 | 支持值 | 影响 |
|---|---|---|
| `footprint.shape` | `rectangle / L / U / T / hex / octagon / custom` | **无原生曲线**，曲面平面必须用 `custom` 多边形折线逼近 |
| `WallNode` | 直线段（`start`→`end`） | 曲墙需切分为多段短直墙 |
| `RoofNode` | 多段平／斜面板，含 `pitch_deg` | 连续曲面屋顶须用多块带不同倾角的 roof slab 拼接 |
| `SlabNode.boundary` | 闭合多边形 | 曲边楼板用多顶点折线近似 |
| 单位 | 全部毫米（mm） | 文档中所有线性量均为 mm |

> 结论：本需求文档把"曲面"统一翻译为**高密度折线逼近**（建议每段
> 1500–2500 mm），并由 `expander` 在生成 BuildingGraph 时落实。

## 3. 总体参数

| 项 | 取值 | 备注 |
|---|---|---|
| 建筑类型 | `cultural_center` | 展览／表演公共建筑 |
| 整体外包络 | ~100 m × 70 m | `footprint.x_mm = 100000`、`footprint.y_mm = 70000` |
| `footprint.shape` | `custom` | 由 ~40 个顶点的折线包络，逼近"水滴回落"曲线 |
| 名义层数 | 3 层（地面 + 中庭层 + 高展厅层） | 实际剖面高度连续变化，按楼层离散化 |
| 地面标高 | 0 mm | — |
| 主导材料（外饰） | `GFRC_White`（白色玻纤增强混凝土） | 退化时映射到 `Concrete` |
| 屋面材料 | `GFRC_White` | 与外墙同材，体现"墙顶一体" |
| 楼板材料 | `Concrete` | 内部结构 |
| 主体结构 | 钢结构（屋面网壳）+ 混凝土核心筒 | 文档级别，IFC 用 `material` 字段表达 |

## 4. 分层需求（SpatialGraph 草案）

### Storey s1 — Ground Floor（公共大厅／门厅）
- `elevation_mm`: 0
- `height_mm`: 6000（首层高净空 6 m，体现大堂高敞）
- `layout_hint`: `atrium`
- 房间（`rooms`）：
  - `lobby`（function=`lobby`, area_ratio=0.35, has_external_facade=true,
    n_external_doors=4, n_windows=0）
  - `exhibition_hall_a`（function=`open_space`, area_ratio=0.30,
    has_external_facade=true, n_windows=0）
  - `auditorium`（function=`conference`, area_ratio=0.20,
    has_external_facade=false）
  - `service_core`（function=`service`, area_ratio=0.10,
    adjacent_to=[lobby, exhibition_hall_a, auditorium]）
  - `cafe`（function=`retail`, area_ratio=0.05, has_external_facade=true,
    n_external_doors=1, n_windows=0）
- `elements`（粗算量，可由 Architect 微调）：
  - walls: 28（核心筒 + 房间隔断；**不含**外曲墙）
  - external_curve_walls: 40（外侧折线墙，每段 ~6 m，沿 ~240 m 周长包络）
  - doors: 12（含主入口 4 扇 + 内部 8 扇）
  - windows: 0（采光全部由曲面玻璃幕墙提供，见 §6）
  - columns: 14（钢柱，分布在大跨展厅）
  - slabs: 1（首层地坪）
  - railings: 4（中庭洞口边缘）

### Storey s2 — Mezzanine Floor（中庭夹层）
- `elevation_mm`: 6000
- `height_mm`: 5000
- `layout_hint`: `atrium`
- rooms：环形走廊 + 4 个小展厅 + 2 个会议室
- elements：
  - walls: 22
  - doors: 8
  - windows: 0
  - columns: 10
  - slabs: 1（局部楼板，中庭挖空）
  - railings: 12（环中庭栏杆，沿曲线折线布置）

### Storey s3 — Upper Hall（高展厅／全景层）
- `elevation_mm`: 11000
- `height_mm`: 8000（顶部曲面在此层升至最高点）
- `layout_hint`: `open_plan`
- rooms：一个大型通高展厅 + 屋顶设备夹层
- elements：
  - walls: 10（仅核心筒延伸）
  - doors: 4
  - windows: 0
  - columns: 6
  - slabs: 1
  - roofs: 8（**关键**：连续曲面屋顶用 8 块带不同 `pitch_deg`
    的 roof slab 拼接，每块 `pitch_deg` ∈ [0°, 18°]，材料 `GFRC_White`）
  - railings: 2

## 5. 几何形态的折线近似策略

由于 `WallNode.start/end` 只接受两点直线，"流线型"必须由 `expander`
在 `custom` 包络多边形上落实：

1. **平面包络**：把建筑投影到 XY 平面，用 ~40 个顶点近似一条
   类似水滴形的闭合曲线（长轴沿 X，弧顶偏一侧）。每段弦长 ~5–6 m。
2. **外墙生成**：把这 40 段弦每段建成一面 `WallNode`，
   `thickness=300`、`is_external=true`、`material=GFRC_White`、`height`
   随楼层（首层 6000、夹层 5000、上层 8000）。
3. **曲面屋顶**：在 s3 顶部用 8 块 `RoofNode`，`boundary` 为相邻外墙
   顶点构成的梯形片段，`pitch_deg` 从弧顶向两端从 0° 渐变至 ~18°；
   材料统一 `GFRC_White`。
4. **可选 stretch goal**：若 `expander` 后续扩展支持 NURBS／曲面网格，
   优先用真实曲面替换上述折线／折面近似。

## 6. 立面与开洞

参考图的标志特征是**外曲面上没有传统意义上的"窗"**，自然光全部
经由屋面缝隙与端部玻璃幕墙引入。

- 外曲墙：`n_windows = 0`，不在外曲墙上开任何 `IfcWindow`。
- 端部玻璃幕墙：建议作为**单独的 `IfcCurtainWall` 扩展**实现（当前
  schema 暂用 walls + 大面积透明 material 表达，命名为
  `curtain_glass_end_west` / `_east`）。
- 入口：4 处主入口集中在西立面底部，作为 `OpeningNode(kind="door")`
  挂在最低处的折线墙段上。

## 7. 材料 / 命名约定

| key | 取值 |
|---|---|
| `wall.material`（外曲墙） | `GFRC_White` |
| `wall.material`（内部隔墙） | `Gypsum_Board` |
| `wall.material`（核心筒） | `Concrete` |
| `slab.material` | `Concrete` |
| `roof.material` | `GFRC_White` |
| `column.material` | `Steel` |
| `railing.material` | `Steel_White_Painted` |

> 注：未在 builder 中识别的自定义材料字符串会回退到 `Concrete`，
> 但材料名仍写入 IFC `IfcMaterial.Name`，供下游 QA 检索（参见
> `ifc_agent/ifc_tools.py` 的 `get_element_material`）。

## 8. 元素总量（汇总，供 `comparator` 评分用）

| 元素 | 总数 |
|---|---|
| storeys | 3 |
| walls | 130（28 + 22 + 10 内部 + 40 + 30 外曲墙近似分摊到三层；以 expander 实算为准） |
| doors | 24 |
| windows | 0 |
| columns | 30 |
| slabs | 3 |
| roofs | 8 |
| railings | 18 |

## 9. 运行方式建议

可用以下命令把本需求作为 prompt 喂给现有管线（先在
`demo_prompts/` 下放一份对应的 `.txt` 摘要，再调用）：

```bash
python run_text2ifc.py \
    --prompt-file demo_prompts/zaha_curved_white_building.txt \
    --iterations 3 \
    --run-name zaha_curved
```

`zaha_curved_white_building.txt` 可放本文件 §1 + §3 的 3–5 句精炼版本，
供 `RequirementsAnalyst` 走 LLM 路径生成 JSON；细节由 Architect 在
迭代中通过 Refiner 反馈逼近本文档的目标量。

## 10. 已知 gap（写给后续维护者）

- 当前 `expander.py` 对 `footprint.shape="custom"` 的处理：需要确认
  其是否接受任意多边形顶点列表；若仅支持枚举形状，需先扩展
  `Footprint` 增加 `boundary: list[Point2D]` 字段（与 `SlabNode` 对齐）。
- 屋面斜率 `pitch_deg` 在 builder 中是否参与几何生成需要确认；如尚未
  实现，应作为 issue 提出，否则连续曲面屋顶降级为多块水平 roof slab。
- 玻璃幕墙类型 `IfcCurtainWall` 当前 schema 未定义，建议补一个
  `CurtainWallNode`，否则只能用透明 material 的 wall 替代。
