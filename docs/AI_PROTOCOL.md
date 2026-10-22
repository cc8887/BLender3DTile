# 面向 AI 的观察协议

目标是让 Agent 同时理解模型结构、几何轮廓与最终表面，并且能证明这些信息属于同一次成功构建。

## 读取层级

`observe_asset(asset_id)` 默认是 `detail=summary`。它返回稳定对象 ID、kit、包围盒、顶点/三角形/骨骼数、图片索引、产物 ID、验证摘要及 freshness。完整 UV/区域/骨架/逐图投影保留在 `detail=full` 和 `inspect_object` 中，避免每轮塞入全部细节。

每张图片包含：

- `revision`：冻结场景的内容版本。
- `path`、`sha256`、尺寸：对应不可变图片。
- `mode`、`camera`、`pose`：解释图像的用途和视角。
- `artifact_id`：服务读取图片的唯一允许句柄，来自已验证 manifest。
- 完整模式下的对象屏幕投影：左上角图像坐标，配合 object ID 图定位。投影矩形不等于可见区域，遮挡仍需看图。

图像用途：

| 图像 | AI 应判断什么 |
|---|---|
| contact_sheet | 先看整体是否符合意图，定位需要细查的视图 |
| beauty | 颜色、像素纹理、材质分区和最终外观 |
| clay | 不受贴图干扰的轮廓、斜面、曲面和关节形状 |
| wireframe | 结构密度、面分布和形变区域 |
| object_id | 将画面区域映射回稳定对象 ID |
| uv_debug | 分区、方向、接缝与拉伸 |
| atlas_uv | 在实际 atlas 上查看真实 UV 边界 |
| pose | 抬臂/屈肘后的轮廓、缺口、穿插和纹理连续性 |

## 版本和修改

```json
{
  "asset_id": "house_demo",
  "revision": "latest",
  "known_revision": "0123456789abcdef",
  "detail": "summary"
}
```

`latest` 只在观察请求开始时解析一次。结果给出明确 revision；后续图片和对象查询必须传这个值，不能重新传 latest。若另一个构建恰好完成，当前观察仍是内部一致的旧快照，下次修改的 `expected_revision` 会阻止覆盖新结果。

提供 `known_revision` 时，服务比较稳定对象列表、世界变换、几何指纹、表面指纹、区域映射与骨骼数量，返回新增/删除/修改对象。表面指纹来源于实际 UV、材质和图片像素，因此同一 tile ID 下重新画图也能识别。共享 atlas 中其他 tile 改变可能保守地将共享该图片的对象标为表面变化；这不影响正确性。

`build_asset` 的 `request_id` 是幂等键；相同 ID 不同内容返回 `E_REQUEST_CONFLICT`。编辑要用新 ID，并附上观察结果的 `next_build_expected_revision`。

## 发布与失败

构建在临时目录完成。服务检查 GLB、观察对象身份、每张图的 revision、SHA-256 和存在性，然后写不可变 revision 目录；最后一次原子替换 latest 指针才算发布成功。

失败时：

- 上一成功 GLB 和图片不变。
- `freshness.latest_attempt_status` 报告失败或正在构建。
- 构建响应给出结构化错误和失败目录，可查看 worker 日志。
- JSON/Schema 在受理前就被拒绝的请求直接返回错误，不把它当成已开始的构建。

所有已发布 artifact 在读取时核对哈希。直接覆盖目录中的 PNG、GLB 或 manifest 会使读取失败，而非悄悄给 AI 返回变更后的内容。

## 诊断与行动范围

错误包含稳定代码，以及适用时的 `op_index`、`object_id`、`region`、`allowed`。Agent 应通过修改允许的配方操作修复问题，例如换兼容 tile、修正引用、先绑定再请求姿态。

如果固定 kit 本身的变形质量不好，应该让开发者修改受信模板与权重规则。不要用新增任意 Python 执行接口绕过工具边界。

观察包是配方构建结果，不是对用户正在操作的 Blender 窗口的实时镜像。将来若需要人工编辑同步，应另设显式的模板接入/重新认证流程。
