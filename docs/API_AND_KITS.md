# API、组件与贴图开发约定

本文件描述当前已实现的 `schema_version=0.2.0`、`catalog_version=starter_v1`。字段和枚举的权威来源是 [request.schema.json](../schemas/request.schema.json) 与 [starter_v1.catalog.json](../catalog/starter_v1.catalog.json)。Agent 开始工作时调用 `get_catalog`，取得当前 Schema 和 `catalog_sha256`，不要从旧聊天复制内容锁。

运行基线为 Python 3.11、Blender/`bpy` 4.5.14。默认构建使用独立 Python 子进程中的 `bpy`；无需启动 Blender 窗口。Kit 由受信代码生成，当前没有从任意 `.blend`、用户脚本或外部网格加载新 kit 的公共接口。

## 对外工具

| 工具 | MCP 参数 | 返回内容与约束 |
|---|---|---|
| `get_catalog` | `{}` | Catalog、SHA-256、完整 Schema、运行基线；只读 |
| `build_asset` | `recipe, request_id, expected_revision?` | 构建完整配方并发布不可变 revision；唯一写工具 |
| `observe_asset` | `asset_id, revision?, known_revision?, detail?` | 默认 `revision="latest"`、`detail="summary"`；可请求 `full` 和版本差异 |
| `inspect_object` | `asset_id, revision, object_id` | 单对象的区域、槽位、几何与定位信息；必须使用明确 revision |
| `read_preview` | `asset_id, revision, artifact_id` | MCP 原生 PNG 图片；必须使用观察结果中的明确 revision 与图片 artifact ID |

JSON 请求文件使用 `method`、`request_id`、`params` 外壳。`build_asset` 的 `params` 就是 MCP 参数中的 `recipe`，`expected_revision` 位于请求顶层。JSON 形式的 `get_catalog` 还要求 `params: {"catalog_version":"starter_v1"}`；MCP 的简化入口无需这个参数。CLI/JSON 的 `read_preview` 返回已验证图片的路径、哈希和类型，MCP 返回图片内容。

ID 格式为 `[a-z][a-z0-9_]{0,47}`；revision 为 16 位小写十六进制字符串。同一 `request_id` 只能对应完全相同的请求。编辑配方时使用新 ID，并把观察所得 revision 放入 `expected_revision`；传 `null` 表示只允许首次构建，省略表示不检查当前版本。

推荐读取顺序为 `get_catalog → build_asset → observe_asset → read_preview → inspect_object`。先看 contact sheet，再按需要查看独立视图。完整的版本一致性、失败状态与图像用途见 [AI_PROTOCOL.md](AI_PROTOCOL.md)。

## 配方合同

配方必需字段为 `schema_version`、`catalog_version`、`catalog_sha256`、`asset_id`、`asset_kind`、`seed`、`texture_profile`、`ops`。`asset_kind` 为 `prop` 或 `character`，`texture_profile` 固定为 `pixel_atlas_v1`；`seed` 为 `0..2147483647` 的整数。

所有请求禁止额外字段、重复 JSON 键、NaN 和 Infinity；JSON 文件上限为 2 MB。一次配方包含 2–256 条指令、至少一个 `place`、最多 128 个实例；总三角形上限为 100,000。每次从空场景重建，不对上一版本做隐式增量修改。

| `op` | 必需参数，均不含表中的 `op` 字段 | 行为与关键限制 |
|---|---|---|
| `place` | `id, kit_id, pos, rot, scale` | 实例化 kit 并应用默认槽位与 UV；ID 唯一 |
| `attach` | `child, child_socket, parent, parent_socket, mate` | `mate="flush"`；对齐双插座并建立父子关系；替代 child 原先的位置/朝向 |
| `assign_slot` | `mesh, region, slot` | 仅允许区域声明的槽位，同时重置为该槽位的默认 tile 和 0° UV 朝向 |
| `set_region_uv` | `mesh, region, tile_id, orientation` | 将预制 chart 映射到兼容 tile；不接受浮点 UV 或原始面索引 |
| `paint_tile` | `tile_id, pattern, palette`；可选 `marks` | 重绘共享 tile；每 tile 每配方最多一次；所有使用者共享效果 |
| `bind_humanoid` | `mesh, preset` | `preset="humanoid_20_v1"`；只接受兼容的人形 kit，每实例绑定一次 |
| `validate` | `profile` | `prop_v1` 或 `character_v1`，必须匹配资产类型；声明最终场景检查 |
| `render_preview` | `camera, pose, texture_view` | 声明最终场景的额外图片；最多 12 条；不捕获指令执行中间态 |
| `export_glb` | `profile, clips` | `profile="game_pixel_v1"`，`clips="none"` 或 `"rig_smoke_v1"`；恰好一次且必须最后执行 |

共同语义：

- 坐标为米、右手系、+Z 向上、-Y 为正面，角色左侧为 +X。`pos=[x,y,z]` 的各分量位于 `[-64,64]`，按 0.125 米网格取值。
- `rot` 仅允许 `yaw_0/90/180/270`；`scale` 为统一正缩放 `0.5/1/2`。斜屋顶和坡道使用真实斜面几何。
- 对象必须先 `place` 再引用。插座只支持静态 kit；双方缩放一致、类型与认证组合匹配，插座不能重复占用，连接不能成环，child 不能二次连接。
- 静态区域允许 `orientation=0/90/180/270`，角色区域只允许 `0`。先改变槽位，再选择该槽位兼容的 tile。
- `paint_tile` 在构建初始化时统一应用，位于 `place` 前后效果相同；`place` 不会覆盖绘画结果。
- `camera` 为 `front/side/back/three_quarter`；`pose` 为 `rest/arms_raise_90/arms_raise_120/elbows_bend_90`；`texture_view` 为 `final/uv_debug`。非静止姿势和测试动画要求已绑定角色。
- `prop` 不含人形 kit；`character` 恰好包含一个人形，并在导出前绑定。当前角色导出检查要求所有导出网格带 skin，因此角色配方应只放置该人形；静态附件与角色混合导出尚未作为支持路径。
- 即使省略 `validate`，导出前后的检查仍执行。图片、GLB 和诊断全部完成并验证后，才更新 `latest`。

完整可执行示例：[房子](../examples/house.request.json)、[丧尸角色](../examples/zombie.request.json)。例如房子中的插接指令为：

```json
{
  "op": "attach",
  "child": "roof",
  "child_socket": "base",
  "parent": "walls",
  "parent_socket": "roof_mount",
  "mate": "flush"
}
```

## 首批 kit

| `kit_id` | 几何与尺寸 | 主要区域、连接 |
|---|---|---|
| `house_shell_2m_v1` | 2×2 米、墙高 2 米；真实拱门和侧窗洞口 | `wall_front/back/left/right`、`floor`、`reveals`；插座 `roof_mount`、`door_mount`、`window_left/right` |
| `roof_gable_35_v1` | 35° 双坡屋顶，0.15 米屋檐，封闭山墙 | `roof_left/right`、`gable_front/back`、`trim`；`base` 接房屋 `roof_mount` |
| `door_roundtop_v1` | 0.75×1.75 米拱门，底部中心原点 | `panel`、`frame`、`handle`；`mount` 接 `door_mount` |
| `window_square_v1` | 0.5×0.5 米窗，中心原点 | `frame`、`glass`；`mount` 接 `window_left/right` |
| `beam_bevel_v1` | 0.1×0.1×1 米立梁，真实倒角 | `body`、`endcap`；无插座 |
| `ramp_30_v1` | 宽 1 米、水平进深 1 米、坡度 30° | `slope`、`body`；无插座 |
| `barrel_12_v1` | 十二边木桶，高 0.6 米，多圈轮廓 | `body`、`bands`、`endcap`；无插座 |
| `humanoid_round_v1` | 连通曲面、固定 A 姿势、436 顶点/434 四边面 | `face/head/neck/torso/pelvis` 与左右上臂、前臂、手、大腿、小腿、脚；无插座 |

角色区域实际名称采用 `upper_arm_l`、`forearm_r`、`thigh_l` 等后缀，完整枚举以 Catalog 为准。默认脸对应 `face` 槽；头、颈、前臂、手对应 `skin`；躯干及上臂对应 `shirt`；骨盆和腿对应 `jeans`；脚对应 `shoes`。只有上臂允许在 `skin` 和 `shirt` 两槽之间切换。

`humanoid_20_v1` 包含根骨、骨盆、脊柱、胸、颈、头，以及左右锁骨、上臂、前臂、手、大腿、小腿、脚。受信解剖包络规则确定性生成归一化顶点权重，每顶点最多 4 个影响；绑定前检查拓扑 SHA-256。权重表会写入 `model_data.json`，当前没有接受用户骨架或自由权重的接口。

## Atlas 与槽位

两张图片均为 256×256、RGB PNG、sRGB。矩形格式为 `(x,y,width,height)`，原点在图片左上角；下表尺寸包含四周各 2 像素的扩色边距。当前每个 slot 只有一个默认及允许的 tile。

| Atlas | `tile_id` | `slot` | 矩形 | 允许的 pattern |
|---|---|---|---|---|
| `environment_v1` | `env_wall_cream` | `wall` | `(0,0,32,32)` | `solid/plaster/checker` |
| `environment_v1` | `env_roof_rust` | `roof` | `(32,0,32,32)` | `solid/roof_shingles` |
| `environment_v1` | `env_wood_oak` | `wood` | `(64,0,32,32)` | `solid/planks` |
| `environment_v1` | `env_stone_slate` | `stone` | `(96,0,32,32)` | `solid/checker` |
| `environment_v1` | `env_iron_dark` | `metal` | `(128,0,32,32)` | `solid` |
| `environment_v1` | `env_glass_blue` | `glass` | `(160,0,32,32)` | `solid` |
| `character_v1` | `char_skin_mint` | `skin` | `(0,0,128,64)` | `solid/skin_speckle` |
| `character_v1` | `char_face_sleepy` | `face` | `(128,0,64,64)` | `face_zombie/face_cute` |
| `character_v1` | `char_shirt_plum` | `shirt` | `(0,64,128,64)` | `solid/stitches` |
| `character_v1` | `char_jeans_indigo` | `jeans` | `(0,128,128,96)` | `solid/denim` |
| `character_v1` | `char_shoes_dark` | `shoes` | `(128,128,64,64)` | `solid` |

默认环境使用 `pastel16`，角色使用 `zombie16`；两套调色板各 16 色，颜色值在 Catalog 中。默认 pattern 除脸为 `face_zombie` 外均为 `solid`；示例配方显式选择更丰富的图案。`glass` 当前为不透明蓝色像素表面，不是透射玻璃材质。

`marks` 最多 2048 项，每项形如 `{"x":0,"y":0,"color_index":5}`。坐标是 tile **内区**的整数像素位置，索引为 `0..15`；例如 32×32 tile 的有效坐标是 `0..27`。先绘制图案，再应用 marks，最后重新生成边缘扩色。

Kit 的 `uv_template` 使用每个 Blender loop 的局部 `(s,t)`，左上原点且范围为 `[0,1]`。材质层负责 V 翻转，并将端点映射到内区首尾像素中心。建筑重复图块使用预制网格单元，角色使用连续 chart 和预留区域矩形；不通过整张 atlas 的 `REPEAT` 做铺砖。

同一 atlas 共享图像和材质。Blender 使用 `Closest`；GLB 固定 `minFilter=magFilter=9728`、`wrapS=wrapT=33071`。最终 PNG 嵌入 GLB，调试 `.blend` 打包图片。UV 调试图片使用彩色格、向上箭头、角标和 tile 序号；观察包还提供最终 atlas 上的真实 UV 边界。

## 目录与模块职责

| 路径 | 职责 |
|---|---|
| `schemas/`、`catalog/` | 公开 Schema、kit/region/slot/tile/socket/rig 定义和受信源文件哈希 |
| `examples/` | 两份完整配方与 MCP 客户端连接示例 |
| `src/b3dtile/contracts.py` | 严格 JSON、Schema、语义兼容性、预算和内容锁检查 |
| `src/b3dtile/service.py` | 进程隔离、构建锁、幂等请求、不可变 revision、原子发布、读取校验 |
| `src/b3dtile/cli.py`、`mcp_server.py` | CLI 与 MCP 适配；不向 Agent 暴露 Blender 状态 |
| `src/b3dtile/worker.py`、`blender_entry.py` | 固定配方解释器及 Blender CLI 启动入口 |
| `src/b3dtile/geometry.py`、`model.py` | 受信几何生成、`KitInstance`、稳定语义区域、UV 模板及拓扑签名 |
| `src/b3dtile/atlas.py`、`materials.py` | 普通 Python 像素绘画；Blender 材质和固定 UV 适配 |
| `src/b3dtile/rig.py` | 固定骨架、权重生成、姿势验证和测试动画 |
| `src/b3dtile/glb.py` | 唯一 GLB 出口、二进制 accessor 检查、CPU 蒙皮及动画采样 |
| `src/b3dtile/observation.py` | 多视图渲染、语义结构、图像定位、摘要与观察包 |
| `src/b3dtile/lifecycle.py` | 受控进程结束，处理 Windows bpy 析构阶段的已知问题 |
| `tools/`、`tests/` | 安装、合同同步、官方 Validator 桥接、单元与回归测试 |
| `out/<asset_id>/revisions/<revision>/` | 成功产物、manifest、GLB、PNG、诊断与复现输入 |

内置 GLB 检查会真实读取二进制 UV、权重和绑定矩阵，并执行静止姿势重建及动画采样；它不等同于 Khronos 官方 Validator。安装对应 Node 工具后，worker 会追加官方检查；未安装时报告 `khronos.status=not_installed`。

## 添加受信 kit 的流程

1. 在 `geometry.py` 添加固定生成器并注册到 `build_kit`；返回 `KitInstance`，明确 root、mesh、完整 regions、每 loop 的 UV 模板和元数据。面必须恰好属于一个区域，UV 必须有限并落在局部 `[0,1]`。
2. 在 Catalog 声明稳定 `kit_id`、尺寸、原点、区域允许槽位/旋转和插座。若新增连接，在 `socket_types` 注册接口与认证的 child/parent 配对；不能仅增加插座名称。
3. 显式更新 Schema 的 kit、region、socket 等枚举；若新增 tile/slot/pattern/palette，还要同步 Catalog、图案实现、材质兼容性与公共枚举。`sync_contract.py` 不会自动推导新增 kit 的全部枚举。
4. 对角色变更，重新设计固定拓扑和 chart，核对骨名/轴/父子关系与权重规则；更新拓扑版本与已实测 SHA-256，并修改同步脚本中相应基线。当前绑定器只支持 `humanoid_round_v1`，新增角色需同步扩展兼容性代码。
5. 添加与风险匹配的检查：区域完整性、斜面/曲面轮廓、接缝、插座变换、槽位切换；角色还需四个姿势的形变和导出后 CPU 蒙皮检查。查看 beauty、clay、wireframe、UV 与姿态图，不能只凭数值通过判断外观。
6. 代码及测试通过后，运行 `.venv\Scripts\python.exe tools\sync_contract.py` 刷新源哈希、目录哈希和现有示例内容锁，审阅修改，再用新的完整配方构建验收。旧配方应重新获取 Catalog，不修改旧 revision 中的产物。

测试入口为 `.venv\Scripts\python.exe tools\run_tests.py -q`。UE/Godot 的实际导入与运行时材质表现仍需要在目标引擎版本中验收；当前没有 FBX 导出、自由拓扑编辑或任意模型自动绑定。
