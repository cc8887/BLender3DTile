# 四模型隔离资产生成测试

基线实现先提交为 `eb048fcd165222f451288ad3e6797c6c9b0917d0`。正式隔离测试发现 Windows MCP 子进程继承标准输入会阻塞启动，修复并补上真实构建回归后，所有模型从修复后的提交重新开始。每轮使用 `run.json` 记录的精确提交快照，不修改生产实现，也不给模型现成配方。

## 统一输入

每次会话的完整输入为 [common.md](prompts/common.md) 加 [house.md](prompts/house.md) 或 [zombie.md](prompts/zombie.md)。任务文本不含模型名字；同任务的四份 prompt 字节和 SHA-256 完全一致。

| 标签 | 固定模型 ID | 推理强度 | 任务 |
|---|---|---|---|
| luna | gpt-6-luna | medium | 房屋、僵尸各一次 |
| terra | gpt-5.6-terra | medium | 房屋、僵尸各一次 |
| sol | gpt-6.1-sol | medium | 房屋、僵尸各一次 |
| astra | gpt-6-astra | medium | 房屋、僵尸各一次 |

模型 ID 来自本机模型目录，并通过 CLI 的 `--model` 显式指定。不用其他模型替代失败路由。若服务端未给出独立的 resolved-model 信息，记录的是明确请求的模型 ID，不伪造服务端证明。

每项固定 seed=20261022、最多三次 build 调用、40 次总工具调用、20 分钟墙钟时间。失败或输入被拒绝的 build 也消耗次数；预算拒绝不再计为已准入 build。没有人为 token 截断。工具定义和输入保持一致，模型自身系统提示、能力与服务端缓存仍可能不同。

## 隔离与过程证据

- 共八个独立、无历史的 CLI 会话，房屋与僵尸也互相隔离。
- 每次运行具有空工作目录、独立 MCP 进程、独立资产目录和独立请求记录。
- CLI 忽略用户配置，并关闭 shell、网页、Apps、插件、hooks、记忆、子代理和主机技能发现。授权只覆盖本轮资产制作。
- 模型通过原有五个 MCP 工具取得目录和构建资产。审计层保持生产工具 Schema 与返回图像，增加目标资产限制及调用预算。其他资产 ID 会被拒绝。
- 完整请求、工具结果摘要、明确 revision、实际图片读取 SHA-256、worker 次数记录在 audit.jsonl；模型输出和 usage 保存在 events.jsonl。
- 审计入口和 runner 也复制并保存 SHA-256；记录 Python、bpy、MCP、CLI、Pillow、jsonschema、Validator 的版本。MCP 调用超时大于 worker 的内部超时。
- backend 源码来自 git archive；复核 catalog 的精确字节哈希，并复制相同版本的官方 glTF Validator。
- 本机账户认证仍由既有 Codex 安装管理；测试不会复制或打印凭证，也不会修改用户配置。

这是上下文、工具访问和产物级隔离，不是独立操作系统或独立账户。八个进程共享同一台机器与已安装运行时，默认最多四个会话并发。耗时包含排队、推理、工具执行与渲染，不作为纯模型速度排名。缓存状态未知；usage 仅使用 CLI 返回的数值，不估算缺失部分。

`skip_host_skill_discovery` 是本机 CLI 的实验开关；实际启用参数保存在各运行目录的 command.json。CLI 配置字段参见[官方配置文档](https://learn.chatgpt.com/docs/config-file/config-reference)。

## 执行

在仓库根目录，使用已经安装依赖的 Python：

```powershell
.venv\Scripts\python.exe benchmarks\run_matrix.py --run-root out\benchmarks\new_matrix
.venv\Scripts\python.exe benchmarks\evaluate.py --run-root out\benchmarks\new_matrix
```

输出目录必须是新的，不覆盖旧试验。可使用 `--prepare-only` 先冻结输入，再用 `--prepared` 启动；`--models` 和 `--tasks` 可用于明确记录的部分试验。不会静默重跑失败模型并只保留最好的一次。

默认冻结当前 `HEAD`，也可以用 `--baseline <commit>` 指定。首轮 `20261022_matrix_v2` 因上述公共基础设施故障整体中止，原日志保留并标为 `infrastructure_aborted`；它不计作任何模型的成功或失败样本。

```text
<run-root>/
  run.json                  # 基线、模型、输入哈希和共同条件
  backend/                  # 固定提交代码和验证器
  prompts/                  # 本轮原始输入与合并后的完整prompt
  runs/<模型>_<任务>/
    run.json                # 状态、明确请求的模型、时间、退出码
    command.json            # 实际启动参数，不包含凭证
    events.jsonl            # CLI事件和返回的usage
    audit.jsonl             # 工具调用及实际图片读取证据
    final.md                # 被测模型最终回答
    assets/                 # 配方、GLB、贴图、预览、诊断及版本
    workspace/              # 模型的空工作目录
```

## 判定边界

机器验收检查 GLB/UV/绑定、指定组件与插座、自定义像素是否实际变化、最终版本图片是否真正被读取、版本更新纪律。美术质量通过相同相机的真实渲染另行查看，不用工具成功数替代。

本工具集的角色体型是固定模板，允许的颜色、图案和组件有限，因此本轮评估的是组件组合、像素设计、工具使用和看图检查能力。每个模型每项只运行一次，结论只适用于本轮，不构成模型整体能力排名。
