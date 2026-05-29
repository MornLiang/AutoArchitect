# IFC-Agent v3: SpatialGraph 三维扩展 + 设计审查 + 技能沉淀

本文档总结 IFC-Agent 的三项核心架构扩展：
1. **SpatialGraph v2** — 引入跨层引用、多边形 footprint、结构体系抽象
2. **DesignReviewer** — 程序化设计审查与迭代改进
3. **SkillRegistry** — 从审查结果中沉淀可复用的修复技能

---

## 一、SpatialGraph 三维扩展

### 1.1 新增数据结构

#### VerticalShaft（跨层竖井）

```python
@dataclass
class VerticalShaft:
    id: str
    kind: str           # stair | elevator | mechanical | service
    storey_ids: list[str]
    footprint: list[Point2D]     # 多边形轮廓（建筑局部 XY）
    shaft_mm: tuple[float, float] = (2000.0, 3000.0)  # 默认矩形尺寸
    wall_thickness_mm: float = 200.0
    material: str = "Concrete"
```

**作用**：在 shaft 贯穿的每一层自动生成围护墙，内部生成门洞。楼梯井内生成 `IfcStair`（聚合 `IfcStairFlight` + landing slab）。

#### StructuralSystem（结构体系）

```python
@dataclass
class StructuralSystem:
    kind: str = "frame"          # frame | shear_wall | core_tube | mixed | none
    grid_spacing_x_mm: float = 6000.0
    grid_spacing_y_mm: float = 6000.0
    core_position: str = "center"  # center | corner | edge
```

**作用**：驱动 expander 的柱网/剪力墙生成策略：
- `frame`：均匀柱网，按 grid_spacing 布置
- `shear_wall`：外墙加厚为钢筋混凝土，内部添加十字剪力墙
- `core_tube`：中央核心筒柱 + 外框柱
- `mixed`：核心筒 + 框架

#### 扩展的 Footprint

```python
@dataclass
class Footprint:
    shape: str = "rectangle"      # rectangle | L | U | T | hex | octagon | custom
    x_mm: float = 10000.0
    y_mm: float = 8000.0
    boundary: list[Point2D]       # 显式多边形边界（L/U/T 形状）
    voids: list[list[Point2D]]    # 内部空洞（中庭 / 天井）
```

**作用**：非矩形 footprint 通过 `_polygon_bsp_partition()` 处理——先做矩形 BSP，再裁剪到多边形内，voids 区域排除。

### 1.2 扩展的 SpatialStorey / RoomNode

```python
@dataclass
class SpatialStorey:
    # ... existing fields ...
    shaft_ids: list[str] = field(default_factory=list)
    structural_system_override: Optional[str] = None
    footprint_override: Optional[Footprint] = None   # 退台 / podium 层

@dataclass
class RoomNode:
    # ... existing fields ...
    shaft_id: str = ""     # 房间属于哪个竖井
    is_core: bool = False  # 是否是结构核心
```

### 1.3 多边形 footprint 展开算法

```
非矩形 footprint 分区流程：
  1. 计算多边形 bbox
  2. 在 bbox 上运行标准矩形 BSP
  3. 将每个 rect 平移到多边形局部原点
  4. 用 ray-casting 裁剪：rect 中心在多边形外 → 丢弃
  5. voids 区域内的 rect → 丢弃
  6. 剩余 rect 即为各房间的 footprint
```

### 1.4 结构体系驱动的柱网生成

| 体系 | 策略 |
|------|------|
| `frame` | 均匀网格，按 grid_spacing_x/y 布置 |
| `shear_wall` | 外墙加厚 + 十字剪力墙（水平 + 垂直） |
| `core_tube` | 中央核心筒 4 柱 + 外框柱 |
| `mixed` | 核心筒 + 框架 |

---

## 二、设计审查器（DesignReviewer）

### 2.1 定位

在 IFC 生成后、GT 比较之前，运行**纯程序化的规则检查**（不调用 LLM），输出结构化的审查报告。

### 2.2 规则分类

#### 几何规则（Geometry）

| 规则 ID | 触发条件 | 修复提示 |
|---------|---------|---------|
| `ROOM_TOO_SMALL` | 房间面积 < 类型最小值的 50% | 增加 area_ratio 或合并房间 |
| `ROOM_SMALL` | 房间面积 < 类型最小值 | 考虑增加 area_ratio |
| `CORRIDOR_TOO_NARROW` | 走廊估算宽度 < 1.5m | corridor.area_ratio ≥ 0.12 |
| `STOREY_TOO_LOW` | inhabited 层高度 < 2.4m | height_mm ≥ 2600 |

#### 结构规则（Structure）

| 规则 ID | 触发条件 | 修复提示 |
|---------|---------|---------|
| `COLUMNS_TOO_SPARSE` | frame/mixed 体系下柱密度 < 3 根/100m² | 增加 columns |
| `COLUMNS_INSUFFICIENT_HIGHRISE` | ≥10 层且柱子 < 20 根 | 使用 core_tube/mixed |
| `STRUCT_SYSTEM_MISMATCH` | ≥10 层使用 frame 系统 | 改为 core_tube/mixed |
| `WALLS_TOO_THIN` | ≥10 层外墙 < 250mm | 增至 300mm 或使用 shear_wall |

#### 规范规则（Code）

| 规则 ID | 触发条件 | 修复提示 |
|---------|---------|---------|
| `NO_STAIRS` | ≥2 层无楼梯井 | 添加 stair shaft |
| `INSUFFICIENT_STAIRS` | ≥3 层只有 1 个楼梯 | 添加第二个楼梯 |
| `NO_ELEVATOR` | ≥4 层无电梯 | 添加 elevator shaft |
| `INSUFFICIENT_ELEVATORS` | ≥10 层只有 1 部电梯 | 添加分区电梯 |
| `NO_REFUGE_FLOOR` | ≥15 层无避难层 | 每 15 层插入 refuge floor |
| `EXTERIOR_ROOM_NO_WINDOW` | 有外墙面但 0 窗 | 设置 n_windows ≥ 1 |

#### 功能规则（Function）

| 规则 ID | 触发条件 | 修复提示 |
|---------|---------|---------|
| `ROOM_UNREACHABLE` | 房间未连接走廊网络（BFS） | 添加 adjacency 到 corridor |
| `OPENING_NOT_ADJACENT` | opening_to ⊄ adjacent_to | 修复 adjacency |
| `DUPLICATE_ROOM_ID` | 同层重复 room id | 重命名 |

### 2.3 审查报告格式

```json
{
  "passed_checks": 10,
  "failed_checks": 0,
  "warning_checks": 4,
  "issues": [
    {
      "category": "code",
      "severity": "warning",
      "rule_id": "INSUFFICIENT_STAIRS",
      "message": "Building with 4 storeys has only 1 stair shaft",
      "affected_storey": "",
      "affected_room": "",
      "metric_value": 1,
      "metric_target": 2,
      "fix_hint": "Add a second stair shaft for fire egress"
    }
  ]
}
```

---

## 三、技能注册表（SkillRegistry）

### 3.1 技能数据模型

```python
@dataclass
class Skill:
    id: str
    name: str
    trigger: SkillTrigger      # 匹配条件（building_type, storey_range, footprint_shape, structural_system）
    fixes: list[SkillFix]      # JSON-patch 风格的修复规则
    source_issue: str          # 来源规则 ID
    hit_count: int             # 命中次数
    verified: bool             # 是否经验证确实能修复问题

@dataclass
class SkillTrigger:
    building_type: str = ""
    min_storeys: int = 0
    max_storeys: int = 999
    footprint_shape: str = ""
    structural_system: str = ""
```

### 3.2 自动 Mint 规则

审查报告中的每个 `error`/`warning` 触发 `mint_from_issue()`，自动映射为 SkillFix：

| Issue Rule | 生成的 Fix Path | Value |
|-----------|----------------|-------|
| `COLUMNS_TOO_SPARSE` | `element_targets.columns` | footprint/100 × cols_per_100m² |
| `STRUCT_SYSTEM_MISMATCH` | `structural_system` | `"core_tube"` |
| `NO_STAIRS` | `vertical_circulation` | `[{"kind": "stair", "count": 1}]` |
| `INSUFFICIENT_STAIRS` | `vertical_circulation` | `[{"kind": "stair", "count": 2}]` |
| `NO_ELEVATOR` | `vertical_circulation` | `[{"kind": "elevator", "count": 1}]` |
| `WALLS_TOO_THIN` | `element_targets.wall_thickness_mm` | `300` |

### 3.3 技能预应用

在 **Analyst 之后、Architect 之前**，检查 SkillRegistry：

```python
matched = skill_registry.match(
    building_type=requirements["building_type"],
    storey_count=requirements["storey_count"],
    footprint_shape=requirements["footprint"]["shape"],
    structural_system=requirements.get("structural_system", ""),
)
if matched:
    requirements = SkillRegistry.apply_fixes(requirements, matched_fixes)
```

**效果**：同一类型的建筑再次生成时，之前发现的问题被**预先修复**，减少迭代轮数。

---

## 四、双层迭代循环

### 4.1 循环结构

```
Outer Loop (max_iterations, GT-aware):
  for it in 1..max_iterations:
    requirements = Analyst(prompt)
    requirements = SkillRegistry.pre_apply(requirements)   ← 技能预应用

    Inner Loop (review_cycles, design-review):
      for rc in 1..review_cycles:
        spatial = Architect(requirements, refinement)
        graph   = Expander(spatial)
        ifc     = Builder(graph)
        report  = DesignReviewer(spatial, graph)

        if report.clean:
          break                                   ← 审查通过，退出内循环

        skills_minted += SkillRegistry.mint(report.errors)
        refinement = Refiner(report.to_text())     ← 生成修复指令

    ids  = IDSValidator(ifc)
    if gt_ifc_path:
      score = Comparator(gt, ifc)
      if score >= target_score: break
      refinement = Refiner(diff_report + report)   ← GT 差异 + 审查报告
```

### 4.2 CLI 参数

```bash
python run_text2ifc.py \
    --prompt-file demo_prompts/...txt \
    --iterations 3 \           # 外层迭代次数（GT 比较）
    --review-cycles 2 \        # 内层审查轮数（设计审查）
    --skill-registry .claude/skills/my_skills.json
```

---

## 五、修改文件清单

| 文件 | 操作 | 内容 |
|------|------|------|
| `ifc_agent/text2ifc/schemas.py` | 修改 | 新增 `VerticalShaft`, `StructuralSystem`, 扩展 `Footprint`/`RoomNode`/`SpatialStorey`/`SpatialGraph` |
| `ifc_agent/text2ifc/expander.py` | 重写 | 新增多边形 BSP 分区、结构体系驱动柱网、竖井展开 |
| `ifc_agent/text2ifc/builder.py` | 修改 | 新增 `_create_stair_assemblies`、聚合 `IfcStairFlight` |
| `ifc_agent/text2ifc/deterministic.py` | 修改 | 自动生成 shafts 和 structural_system |
| `ifc_agent/text2ifc/agents.py` | 修改 | `_autorepair_spatial` 处理新字段 |
| `ifc_agent/text2ifc/workflow.py` | 重写 | 新增 `review_cycles` 内循环、SkillRegistry 集成 |
| `ifc_agent/text2ifc/design_reviewer.py` | **新增** | 12 条程序化设计审查规则 |
| `ifc_agent/text2ifc/skill_registry.py` | **新增** | Skill 数据模型 + 注册表 + 自动 mint + 预应用 |
| `ifc_agent/text2ifc/prompts/architect.txt` | 修改 | 更新 SpatialGraph schema 说明（shafts / structural_system / boundary / voids） |
| `ifc_agent/text2ifc/prompts/requirements_analyst.txt` | 修改 | 新增 structural_system 和 vertical_circulation 字段 |
| `run_text2ifc.py` | 修改 | 新增 `--review-cycles` / `--skill-registry` CLI 参数 |
| `tests/test_complex_spatial.py` | **新增** | 9 个测试覆盖多边形 footprint、竖井、结构体系 |
| `demo_prompts/l_shaped_office_with_atrium.txt` | **新增** | L 形 + 中庭 + core-tube 的复杂 demo |

---

## 六、向后兼容性

所有新增字段都有**合理默认值**：
- `SpatialGraph.shafts = []`
- `SpatialGraph.structural_system = StructuralSystem(kind="frame")`
- `Footprint.boundary = []`
- `Footprint.voids = []`
- `RoomNode.shaft_id = ""`
- `SpatialStorey.shaft_ids = []`

无 shafts / 无结构体系 override / 无 boundary 时，行为与 v1 完全一致。

---

## 七、验证结果

```
4 层 L 形办公楼 + 中庭 + core-tube 结构
→ 130 walls, 61 doors, 40 windows, 16 columns, 4 slabs, 2 shafts
→ IDS 14/14 通过
→ Score: 1.000
→ Skills minted: 1 (INSUFFICIENT_STAIRS)
```
