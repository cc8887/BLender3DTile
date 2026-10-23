# Blender3DTile

面向 AI 的受控低模资产工具集。Agent 只提交枚举化 JSON 配方；可信 Python 实现调用独立 `bpy` worker，生成 GLB、像素 atlas、绑定数据、预览和版本化诊断。

## 已实现

- 8 个程序化 kit：带门窗洞口的房屋、35°双坡屋顶、拱门、窗、倒角立梁、30°坡道、木桶、连通曲面人形。
- 两张 256×256 atlas、16 色调色板、10 种像素图案、分区内像素修改、2px 扩色。
- 固定语义区域与 UV 模板；角色连续 chart；禁止 Agent 传入顶点或浮点 UV。
- 20 骨预设、确定性权重、四个姿态检查、测试动画和 GLB CPU 蒙皮检查。
- 固定相机的外观、白模、线框、对象 ID、UV 和姿态预览。
- 不可变构建 revision、内容哈希、原子 latest 指针、幂等请求和过期版本保护。
- CLI 与薄 stdio MCP 服务；完整枚举 Schema 直接出现在 MCP 工具定义中。

组件目前由受信程序生成，无需先制作 `.blend`。Blender 桌面程序不是必需依赖，默认使用官方 `bpy` 模块。也提供 Blender CLI 启动适配器。

## 安装

基线固定为 Python 3.11、`bpy==4.5.14`。Windows PowerShell：

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -e '.[backend,mcp,test]'
npm.cmd install --prefix .tools/gltf-validator --no-audit --no-fund gltf-validator@2.0.0-dev.3.10
```

或者运行 `tools/bootstrap.ps1`。Node/npm 只用于 Khronos 官方 glTF Validator。未安装它时，内置检查仍执行，但报告会明确标记 `khronos.status=not_installed`，不声称通过官方验证。

## 构建与查看

```powershell
.venv\Scripts\b3dtile.exe check examples\house.request.json
.venv\Scripts\b3dtile.exe build examples\house.request.json
.venv\Scripts\b3dtile.exe build examples\zombie.request.json
.venv\Scripts\b3dtile.exe observe house_demo
.venv\Scripts\b3dtile.exe observe zombie_demo --full
```

默认输出在 `out/<asset_id>/revisions/<revision>/`，CLI 返回实际绝对目录。每个成功构建包括：

```text
<asset_id>.glb          # 嵌入 PNG、材质、网格；角色包含 skin 与动画
debug.blend            # 可人工打开检查，纹理已打包
observation.json       # 图文同一 revision 的完整观察包
manifest.json          # 每个产物的 SHA-256、大小、类型与构建来源
validation.json        # 几何、UV、骨架、GLB 与官方验证结果
model_data.json        # 区域面索引、权重表和固定拓扑信息
request.json           # 本次完整配方
textures/              # 最终 atlas 与诊断 atlas
observations/          # 多视角图片、姿态图片、总览 contact sheet
worker.log
```

同一 `request_id` 只允许同一输入。编辑配方后使用新的 ID，或 CLI 的 `--new-request`：

```powershell
.venv\Scripts\b3dtile.exe build examples\house.request.json --new-request --expected-revision <上次观察到的16位revision>
```

`--expected-revision` 防止基于旧模型修改已更新资产。首次构建的 JSON API 可传 `expected_revision: null`；省略该字段表示不做比较。

完整 CLI 备用入口：

```powershell
.venv\Scripts\b3dtile.exe build examples\house.request.json --backend cli --blender-executable 'C:\Program Files\Blender Foundation\Blender 4.5\blender.exe'
```

此路径也要求精确的 Blender 4.5.14；默认 `bpy` 后端是本项目的验证基线。

## AI 如何读取最新结果

详见 `docs/AI_PROTOCOL.md`。建议循环：

1. `get_catalog` 获取当前枚举及内容锁。
2. `build_asset` 提交完整配方。
3. `observe_asset` 默认返回紧凑摘要，明确 latest 成功版本和最近被接受的构建尝试状态。
4. 用返回的 **明确 revision** 和 contact sheet 的 `artifact_id` 调用 `read_preview`，获取原生图片。
5. 按稳定对象 ID 调用 `inspect_object`，读取区域、槽位、骨架和屏幕定位。
6. 修改配方；提交新请求 ID 和 `expected_revision`。

图片读取不接受 `latest`，避免“先读到 A 版摘要，再取到 B 版图片”。渲染来自冻结后的最终场景，配方里的 `render_preview` 是额外视图声明，不保存中间操作状态。

构建失败不会替换上一版模型。`freshness.latest_attempt_status` 会报告失败；AI 应把保留图片理解为上一成功版本。外部编辑 `debug.blend` 不会自动更新观察包；必须重新构建配方。

## MCP

```powershell
.venv\Scripts\b3dtile-mcp.exe --output-root 'G:\Github\BLender3DTile\out'
```

客户端连接片段见 `examples/mcp-config.json`。本仓库不会自动修改你的客户端配置。

工具：`get_catalog`、`build_asset`、`observe_asset`、`inspect_object`、`read_preview`。MCP 服务不导入 `bpy`，耗时构建运行在独立子进程，图片通过 MCP ImageContent 返回。

## 开发与约定

API 参数、组件目录、atlas 分区和扩展流程见 [API_AND_KITS.md](docs/API_AND_KITS.md)。

配方 `0.3.0` 已增加区域顶点色、atlas 逐帧 UV 动画、20/16/12 骨人形及两种权重预设。接口、GLB 播放器要求、Crocotile/Rigify 来源接入和离线 GLB 提取工具见 [FEATURES_AND_SOURCES.md](docs/FEATURES_AND_SOURCES.md)。可运行 `examples/features_*.request.json` 查看示例；顶点色单独诊断图和 UV 时间序列会进入版本化观察包。

- 核心模块 `contracts.py`、`service.py`、`atlas.py` 不依赖 Blender 状态；Blender 操作集中于后端模块。
- 配方、kit、tile、region、slot、socket、pose、camera 都由 `schemas/request.schema.json` 和 `catalog/starter_v1.catalog.json`约束。
- 坐标：米、右手系、+Z 向上、-Y 为正面。旋转首批为四个 yaw；缩放为统一正缩放 0.5/1/2。
- 插座对齐同时校验认证的组件组合及接口尺寸；同一连接中实例缩放必须一致。
- atlas 矩形原点在左上，UV 转换内部完成。重复使用 tile 合法；跨区/错槽不合法。
- 默认材质在构建开始初始化一次，放置组件不会覆盖已画图案。
- 角色绑定前校验目录中锁定的拓扑签名。固定权重规则生成顶点 ID 权重表，最多 4 个影响；不使用自动热力权重。
- 调整受信组件/权重/atlas实现后，需要审阅并运行 `tools/sync_contract.py` 更新目录哈希和所有示例；现有旧配方需重新取得目录。
- `export_glb` 是唯一资产导出指令，必须恰好一次且在末尾。执行前后都验证，辅助相机/灯光不会混入 GLB。

```powershell
.venv\Scripts\python.exe tools\run_tests.py -q
```

单元与回归测试覆盖非法指令、UV/贴图、真实 bpy 几何与蒙皮、GLB 二进制/CPU 蒙皮、发布中断、篡改检测、版本一致性和真实 MCP stdio。

本机验证发现 Windows `bpy 4.5.14` 在渲染后的 DLL 退出清理阶段可能崩溃，普通 `os._exit` 仍会触发。独立 worker 在全部文件关闭、结果写入后通过 Windows `TerminateProcess` 结束自身，保留实际成功或失败退出码；测试入口同样保留 pytest 的真实退出码。它不忽略运行中崩溃、超时或测试失败，也不把失败任务发布成成功。

仍需在用户实际使用的 UE/Godot 版本中完成导入和运行时材质验收。当前首批角色只有固定体型、贴图服装与测试动作；未提供自由拓扑编辑、独立服装布料、任意自动绑定或 FBX 导出。

示例角色的 Khronos 报告保留一条 `NODE_SKINNED_MESH_NON_ROOT` 警告。glTF 蒙皮遵循关节变换，忽略网格节点自身变换，详见[官方规范](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#joint-hierarchy)。固定导出器已将绑定形状变换烘焙进顶点；回归测试将平移、旋转和缩放后的 GLB 动画顶点与 Blender 实际求值的世界坐标逐帧比较，不仅检查字段是否存在。

同一资产的并发构建由 `.build.lock` 拒绝。进程被强制终止后若留下锁，应先核对锁内 PID 已退出，再手工移除该单个锁文件；不要删除仍在工作的锁。

## 模型隔离测试

Luna、Terra、Sol、Astra 的统一房屋/僵尸 prompt、隔离执行器和结果评估见 [benchmarks/README.md](benchmarks/README.md)。每个模型的每项任务使用全新上下文与独立产物目录，只通过五个受限 MCP 工具操作。测试不会读取现成示例配方，也不会修改生产源码。
