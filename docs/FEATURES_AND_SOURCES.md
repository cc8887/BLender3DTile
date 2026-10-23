# 顶点色、UV 动画与固定人形预设

当前配方版本为 `0.3.0`。新增操作通过原有 `build_asset` 提交；无需增加任意脚本入口。修改前重新读取 `get_catalog`，旧配方内容锁会拒绝新 Catalog。

## 顶点色

```json
{"op":"paint_vertex_color","mesh":"walls","region":"wall_front","style":"height_gradient","palette":"pastel16","color_a":12,"color_b":5}
```

`style` 仅为 `solid` 或 `height_gradient`。颜色索引为 0–15，来自已有两套调色板。渐变按区域内局部 Z 的最低点到最高点插值；水平区域使用 color_a。颜色由 sRGB 转为线性后存储。未涂区域为白色，alpha 固定 1；颜色乘以 atlas 颜色，不能通过该操作增亮到超出纹理颜色或创建透明材质。

底层使用 CORNER 色属性保留区域边界，导出器拆分必要的顶点并写入 `COLOR_0`。这是真实顶点色，不是把 tint 烘焙进 PNG。材质副本按对象隔离；多实例共用一张 atlas 时不会互相改色。

观察包提供 `vertex_color_three_quarter_rest.png`，用不受灯光影响的顶点色诊断材质区分 tint 与纹理；未涂区域显示白色。beauty 图保留最终纹理与光照。

## UV 动画

```json
{"op":"set_uv_animation","mesh":"window_r","region":"glass","preset":"signal_cycle_v1","fps":2}
```

第一批是受控的 atlas 逐帧动画：`signal_blink_v1`（蓝/暗）与 `signal_cycle_v1`（蓝/琥珀/暗/琥珀）。仅兼容 `glass` 槽，fps 为 1/2/4/8；每区域一个 track，每资产最多四个。先设置槽位与 UV，再添加动画；之后不能修改该区域的槽位或 UV。首帧统一使用预设首瓦片与 0° 朝向。

每个动画区域获得独立材质，防止同 atlas 的静态区域一起跳帧。实际 glTF 动画使用 `KHR_animation_pointer` 驱动 `KHR_texture_transform.offset`，时间插值为 STEP；GLB 中嵌入纹理和动画数据，并额外输出可读的 `uv_animation.json`。普通、不支持这些扩展的查看器仍能显示静态首帧；循环播放由宿主播放器控制。没有把 UV 动画伪装为骨骼动画或超范围 UV。

该扩展组合依据 [KHR_animation_pointer](https://github.com/KhronosGroup/glTF/blob/main/extensions/2.0/Khronos/KHR_animation_pointer/README.md) 和 [KHR_texture_transform](https://github.com/KhronosGroup/glTF/blob/main/extensions/2.0/Khronos/KHR_texture_transform/README.md)。UE/Godot 的具体导入器是否播放这些扩展需要单独实测；不能由 GLB 格式校验通过推断引擎可直接播放。JSON 时间线可供目标引擎材质适配器使用。

当前不包含任意连续 UV 滚动、UV 变形或外部翻页纹理导入。扩充 flipbook 必须先在 Catalog 注册等尺寸、有边距且位于同 atlas 的帧，再注册枚举预设。

观察包会渲染每个预设帧开始时刻，图片含 `time_seconds` 和 `uv_frames`，另提供 `uv_animation_strip.png`。所有图片仍与明确 revision 绑定；读取普通 beauty 图只代表首帧，不代表已经检查整个 UV 动画。

## 固定骨架与权重

```json
{"op":"bind_humanoid","mesh":"zombie","preset":"humanoid_compact_16_v1","weight_preset":"anatomical_crisp_v1"}
```

| 预设 | 骨数 | 设计 |
|---|---:|---|
| `humanoid_20_v1` | 20 | 原有完整低模人形，保留锁骨、胸、颈、手和脚 |
| `humanoid_compact_16_v1` | 16 | 胸与锁骨并入脊柱，颈并入头；保留独立手脚 |
| `humanoid_basic_12_v1` | 12 | 在 16 骨基础上，手并入前臂、脚并入小腿 |

这三套是本项目自定义的标准化游戏人形预设，均匹配 `humanoid_round_v1` 的固定 A 姿势、单位与拓扑；不宣称等同 UE Mannequin、Mixamo 或 Rigify 的骨架标识，也不自动完成重定向。

两种权重规则：`anatomical_smooth_v1` 是原有解剖包络平滑权重；`anatomical_crisp_v1` 合并骨骼影响后做固定 1.35 次幂锐化再归一化。省略 `weight_preset` 使用 smooth。所有组合限制每顶点最多 4 个影响，并检查静止、90°/120° 抬臂、90° 弯肘和导出后的 CPU 蒙皮。

Catalog 提供骨名、父子关系和静止坐标；`model_data.json` 保存实际逐顶点权重、拓扑哈希、骨架和权重预设及权重表哈希。观察结果也展示所选预设，权重变化会进入版本差异报告。

## 从其他软件获取什么

需要纠正早期假设：**Crocotile 3D 当前支持骨架、顶点权重和顶点色。** 官方还提供 UV 动画导出，但使用额外 JSON 和特殊 UV 编码，需要运行端解码。[官方文档](https://www.crocotile3d.com/howto.html)

可让 Crocotile 输出带蒙皮的 GLB，作为受信 kit 的来源；导出时保留蒙皮，关闭静态姿势烘焙。其 UV 动画需要单独转换成我们的时间线，不能直接把编码后的 UV 当作常规 atlas UV。

[Blender Rigify](https://docs.blender.org/manual/en/latest/addons/rigging/rigify/basics.html) 提供基础人形等 meta-rig，可在离线制作阶段生成控制骨架并绑定模型。游戏导出应整理形变骨骼与烘焙后的动画；控制骨、约束与绑定权重需要审阅，不自动成为本项目的 runtime preset。

可以复用的是“骨架 + 对应网格 + 对应权重”的完整资产，或骨架设计。权重不能仅凭骨名从任意网格迁移：顶点顺序、拓扑、静止姿态或绑定矩阵不同，就需要重新绑定/转移权重并做形变验证。

建议的接入流程：来源 GLB → 离线提取网格/骨架/权重数据 → 统一坐标、骨名及 UV/区域 → 认证拓扑和姿态 → 注册固定 kit/preset 枚举。来源名称及资产授权说明随模板保存。Agent 运行时只能选择已认证的枚举；不开放任意模型文件、外部脚本或自由权重入口。

本轮内置预设由项目自行实现；没有声称已经导入或实测某份 Crocotile/Rigify 资产。

已提供离线提取工具，读取嵌入式 GLB 的实际二进制网格、UV、顶点色、骨架、绑定矩阵、权重和动画，保存来源 SHA-256 与拓扑指纹：

```powershell
.venv\Scripts\python.exe tools\extract_source_glb.py path\to\source.glb --output out\intake\source_review.json --source-label Crocotile --license-note "自己的资产"
```

输出标记为 `certified=false`，保留 glTF 坐标和源 primitive 的顶点顺序；不转移权重、不自动注册 kit。源纹理仍需保留原 GLB。遇到超出 `[0,1]` 的 UV 会保留原值并提示可能的平铺或动画编码，不擅自截断。这个工具已用生成的 GLB 测试，尚未用真实 Crocotile 文件测试。

## 示例

```powershell
.venv\Scripts\b3dtile.exe build examples\features_house.request.json --new-request
.venv\Scripts\b3dtile.exe build examples\features_rig_20.request.json --new-request
.venv\Scripts\b3dtile.exe build examples\features_rig_16.request.json --new-request
.venv\Scripts\b3dtile.exe build examples\features_rig_12.request.json --new-request
```

房屋示例包含墙面顶点渐变和右窗 UV 动画；角色示例分别演示三种骨架及上衣顶点色。已有版本时，沿用观察协议提交正确的 `expected_revision`。
