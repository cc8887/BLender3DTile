"""Build an offline gallery of the exact revisions named by evaluation.json.

The HTML references existing PNG/GLB artifacts using contained relative URLs.
It does not render, modify images, fetch external resources, or include raw logs.
Run again after evaluate.py to refresh this static snapshot.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import quote


ALIASES = ("luna", "terra", "sol", "astra")
TASKS = {"house": "房屋", "zombie": "僵尸"}
VIEWS = {
    "beauty": ("外观", "beauty", "three_quarter", "rest"),
    "clay": ("白模", "clay", "three_quarter", "rest"),
    "uv": ("UV 分区", "uv_debug", "three_quarter", "rest"),
    "pose": ("角色抬臂 120°", "beauty", "front", "arms_raise_120"),
}


def load_object(path: Path) -> tuple[dict, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value, None
    except FileNotFoundError:
        return {}, "文件尚未生成"
    except (OSError, ValueError):
        return {}, "文件无法读取或 JSON 无效"


def relative_artifact(root: Path, directory: Path, value, suffix: str,
                      expected_sha256=None) -> str:
    """Allow only actual files below this revision, never external/root URLs."""
    if not isinstance(value, str) or not value:
        raise ValueError("产物路径缺失")
    path = (directory / value).resolve()
    if (not directory.resolve().is_relative_to(root)
            or not path.is_relative_to(directory.resolve())
            or not path.is_relative_to(root)
            or path.suffix.lower() != suffix or not path.is_file()):
        raise ValueError("产物缺失、类型不符或路径越界")
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError("图片校验和与 observation 不一致")
    return quote(path.relative_to(root).as_posix(), safe="/")


def acceptance(run: dict, infrastructure_aborted: bool) -> tuple[str, str, str]:
    status = run.get("status", "missing")
    if infrastructure_aborted or run.get("evaluation_status") == "excluded_infrastructure":
        return "neutral", "基础设施中止 · 已排除", "本轮不纳入模型比较。"
    if status in ("missing", "pending", "running", "unknown"):
        return "neutral", "运行未完成 · " + str(status), "尚无最终验收结论，已发布图片也只是当前快照。"
    if run.get("eligible_for_comparison") is False:
        return "neutral", "未纳入验收", "任务被中断或未满足评估前提。"
    if run.get("machine_gates_passed") is True:
        return "pass", "技术验收通过", "机器门槛通过，不代表主观美术评分。"
    if status == "failed":
        return "fail", "执行失败", "请结合验收报告区分执行错误与资产问题。"
    return "fail", "部分验收未通过", "已发布资产仍可查看；未通过项列在下方。"


def card(root: Path, alias: str, task: str, run: dict, model: str,
         evaluation_error: str | None, infrastructure_aborted: bool) -> str:
    task_name = TASKS[task]
    tone, status, status_note = acceptance(run, infrastructure_aborted)
    if evaluation_error and not infrastructure_aborted:
        tone, status, status_note = "neutral", "尚未评估", "评估文件可用后重新生成此页面。"
    revision = run.get("revision")
    valid_revision = isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{16}", revision)
    observation = {}
    directory = root / "runs" / f"{alias}_{task}" / "assets" / f"{task}_eval" / "revisions" / str(revision)
    reason = "尚未发布成功资产。"
    if evaluation_error:
        reason = f"evaluation.json：{evaluation_error}；尚未选择经评估的版本。"
    elif not valid_revision:
        reason = "评估快照未记录成功 revision；任务可能尚未开始、仍在运行或构建失败。"
        calls = run.get("tool_calls", {})
        failures = calls.get("errors", []) if isinstance(calls, dict) else []
        builds = [item for item in failures if isinstance(item, dict) and item.get("tool") == "build_asset"] if isinstance(failures, list) else []
        codes = {item["error"]["code"] for item in builds if isinstance(item.get("error"), dict)
                 and isinstance(item["error"].get("code"), str)
                 and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item["error"]["code"])}
        if codes:
            # Only expose diagnostic codes, never raw log/error message text.
            reason = f"{len(builds)} 次构建调用失败，未发布成功资产。错误代码：" + "、".join(sorted(codes)) + "。"
        if run.get("eligible_for_comparison") is True and not infrastructure_aborted:
            tone, status, status_note = "fail", "未生成资产", "此任务没有可展示或下载的成功产物。"
    else:
        # Also reject a revision directory redirected outside this run by a link.
        if (not directory.resolve().is_relative_to(root)
                or not (directory / "observation.json").resolve().is_relative_to(directory.resolve())):
            reason = "版本目录越界，未读取该资产。"
        else:
            observation, error = load_object(directory / "observation.json")
            if error:
                reason = f"指定 revision 的 observation.json：{error}。"
                observation = {}
            elif observation.get("revision") != revision or observation.get("asset_id") != f"{task}_eval":
                reason = "observation 的资产或版本不匹配，未展示图片。"
                observation = {}
            else:
                reason = "此版本未提供所选视图。"

    images = observation.get("images", [])
    images = images if isinstance(images, list) else []
    choices = {}
    for name, (label, mode, camera, pose) in VIEWS.items():
        if name == "pose" and task == "house":
            mode, camera, pose = "beauty", "three_quarter", "rest"
            label = "房屋保持静态外观"
        image = next((item for item in images if isinstance(item, dict)
                      and (item.get("mode"), item.get("camera"), item.get("pose")) == (mode, camera, pose)
                      and item.get("revision") == revision), None)
        url, failure = "", reason
        if image:
            try:
                url = relative_artifact(root, directory, image.get("path"), ".png", image.get("sha256"))
            except ValueError as exc:
                failure = str(exc)
        camera_label = "三分之四视角" if camera == "three_quarter" else "正面"
        pose_label = "抬臂 120°" if pose == "arms_raise_120" else "静止"
        choices[name] = {"src": url, "label": f"{label} · {camera_label} · {pose_label}", "reason": failure}

    links = []
    if observation:
        for field, suffix, label, download in (
            ("glb", ".glb", "下载 GLB", True),
            ("contact_sheet", ".png", "完整 Contact Sheet", False),
        ):
            try:
                url = relative_artifact(root, directory, observation.get(field), suffix)
            except ValueError:
                links.append(f'<span class="unavailable">{escape(label)}不可用</span>')
            else:
                attrs = ' download' if download else ' target="_blank" rel="noopener"'
                links.append(f'<a href="{escape(url, quote=True)}"{attrs}>{escape(label)} ↗</a>')

    data = " ".join(f'data-{name}-{key}="{escape(value, quote=True)}"'
                    for name, choice in choices.items() for key, value in choice.items())
    default = choices["beauty"]
    alt = f"{alias.title()} · {task_name} · {default['label']}"
    if default["src"]:
        preview = (f'<a class="image-link" href="{escape(default["src"], quote=True)}" target="_blank" rel="noopener">'
                   f'<img src="{escape(default["src"], quote=True)}" alt="{escape(alt, quote=True)}" loading="lazy">'
                   '</a><p class="placeholder" hidden></p>')
    else:
        preview = ('<a class="image-link" target="_blank" rel="noopener" tabindex="-1"><img hidden alt=""></a>'
                   f'<p class="placeholder">{escape(default["reason"])}</p>')
    failed = run.get("failed_gates", [])
    failed = [item for item in failed if isinstance(item, str)] if isinstance(failed, list) else []
    gate_text = ""
    if failed and run.get("eligible_for_comparison") is True:
        gate_text = ('<details class="gates"><summary>查看未通过项</summary><ul>'
                     + "".join(f"<li>{escape(item)}</li>" for item in failed)
                     + '</ul></details>')
    requested = run.get("requested_model") or model or "unknown"
    revision_text = revision if valid_revision else "尚无成功版本"
    return f'''<article class="asset-card" {data} data-title="{escape(alias.title() + ' · ' + task_name, quote=True)}">
  <div class="card-top"><h3>{task_name}</h3><span class="badge {tone}">{escape(status)}</span></div>
  <div class="meta"><span>requested_model</span><code>{escape(str(requested))}</code></div>
  <div class="meta"><span>revision</span><code>{escape(str(revision_text))}</code></div>
  <figure><div class="preview">{preview}</div><figcaption>{escape(default['label'])}</figcaption></figure>
  <div class="artifact-links">{' '.join(links) or '<span class="unavailable">资产链接尚不可用</span>'}</div>
  <p class="status-note">{escape(status_note)}</p>{gate_text}
</article>'''


STYLE = """
:root{color-scheme:light;--ink:#203044;--muted:#627286;--line:#dce4ed;--paper:#fff;--accent:#3067c7}
*{box-sizing:border-box}body{margin:0;background:#eef2f7;color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1280px;margin:auto;padding:38px 28px 48px}header{margin-bottom:22px}.eyebrow{font-size:12px;letter-spacing:.16em;color:var(--accent);font-weight:700}
h1{font-size:30px;line-height:1.25;margin:8px 0 12px}header p{margin:8px 0;color:var(--muted);max-width:950px}.snapshot{font-size:12px;overflow-wrap:anywhere}
.notice{border-left:4px solid #d39736;background:#fff5df;padding:12px 16px;margin:16px 0;color:#765012}
.toolbar{position:sticky;top:0;z-index:5;padding:14px 0;background:#eef2ffed;backdrop-filter:blur(8px);display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-bottom:22px}
.toolbar-label{font-weight:650;margin-right:8px}button{font:inherit;border:1px solid #c6d2e2;color:var(--ink);background:#fff;border-radius:8px;padding:8px 16px;cursor:pointer}
button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}button:hover{filter:brightness(.96)}button:focus-visible,a:focus-visible{outline:3px solid #82affb;outline-offset:3px}
.model-row{display:grid;grid-template-columns:116px minmax(0,1fr) minmax(0,1fr);gap:18px;margin-bottom:22px;align-items:start}.model-label{padding-top:20px}.model-label h2{font-size:24px;margin:0}.model-label small{display:block;font-size:11px;letter-spacing:.08em;color:var(--muted);margin-top:4px}
.asset-card{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 5px 18px #233a5710;min-width:0}.card-top{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px}h3{font-size:18px;margin:0}.badge{font-size:11px;line-height:1.4;border-radius:20px;padding:5px 9px;text-align:center}.pass{color:#176440;background:#e3f5eb}.fail{color:#8c4b17;background:#fff0dd}.neutral{color:#546478;background:#eef2f6}
.meta{display:flex;gap:9px;align-items:baseline;font-size:11px;flex-wrap:wrap;color:var(--muted)}.meta span{min-width:100px}.meta code{font-size:12px;color:#394d64;overflow-wrap:anywhere}
figure{margin:14px 0}.preview{aspect-ratio:1;display:grid;place-items:center;border-radius:10px;background:#e9eef6;overflow:hidden;position:relative}.image-link{display:block;width:100%;height:100%}.image-link:has(img[hidden]){display:none}.preview img{width:100%;height:100%;object-fit:contain;display:block}.preview img[hidden]{display:none}.placeholder{padding:28px;max-width:100%;text-align:center;color:var(--muted);position:absolute;inset:0;display:grid;place-items:center;margin:0}.placeholder[hidden]{display:none}figcaption{font-size:12px;color:var(--muted);margin-top:8px}
.artifact-links{display:flex;gap:16px;flex-wrap:wrap;font-size:13px}a{color:var(--accent);text-underline-offset:3px}.unavailable{color:#8a96a6;font-size:12px}.status-note{font-size:12px;color:var(--muted);margin:10px 0 0}.gates{font-size:11px;color:#7c592d;margin-top:9px}.gates summary{cursor:pointer}.gates ul{padding-left:18px;overflow-wrap:anywhere}footer{border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:12px}footer p{margin:6px 0}noscript{display:block;padding:12px;background:#fff5df}
@media(max-width:900px){main{padding:24px 16px}.model-row{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}.model-label{grid-column:1/-1;padding-top:0;display:flex;align-items:baseline;gap:12px}.model-label h2{font-size:22px}.model-label small{margin:0}.asset-card{padding:14px}.card-top{align-items:flex-start;flex-direction:column;gap:6px}}
@media(max-width:580px){h1{font-size:25px}.model-row{grid-template-columns:1fr;gap:14px;margin-bottom:30px}.toolbar{gap:6px}.toolbar-label{width:100%;font-size:13px}button{font-size:13px;padding:7px 10px}.card-top{flex-direction:row;align-items:center}.meta{font-size:11px}.model-label{margin-bottom:0}}
"""


SCRIPT = """
function showView(view) {
  document.querySelectorAll('.toolbar button').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.view === view));
  });
  document.querySelectorAll('.asset-card').forEach(card => {
    const src = card.dataset[view + 'Src'];
    const label = card.dataset[view + 'Label'];
    const image = card.querySelector('img');
    const link = card.querySelector('.image-link');
    const placeholder = card.querySelector('.placeholder');
    card.querySelector('figcaption').textContent = label;
    image.alt = card.dataset.title + ' · ' + label;
    if (src) {
      image.src = src; image.hidden = false; link.href = src; link.tabIndex = 0;
      placeholder.hidden = true;
    } else {
      image.removeAttribute('src'); image.hidden = true; link.removeAttribute('href'); link.tabIndex = -1;
      placeholder.textContent = card.dataset[view + 'Reason']; placeholder.hidden = false;
    }
  });
}
document.querySelectorAll('.toolbar button').forEach(button => {
  button.addEventListener('click', () => showView(button.dataset.view));
});
document.querySelectorAll('.asset-card img').forEach(image => {
  image.addEventListener('error', () => {
    image.hidden = true;
    const placeholder = image.closest('.asset-card').querySelector('.placeholder');
    placeholder.textContent = '无法读取本地 PNG，文件可能被移动。'; placeholder.hidden = false;
  });
});
"""


def generate(run_root: Path) -> Path:
    root = run_root.resolve()
    if not root.is_dir():
        raise ValueError("Run root does not exist")
    evaluation, error = load_object(root / "evaluation.json")
    config, _ = load_object(root / "run.json")
    models = config.get("models", {})
    models = models if isinstance(models, dict) else {}
    runs = evaluation.get("runs", [])
    runs = runs if isinstance(runs, list) else []
    by_task = {(item.get("alias"), item.get("task")): item for item in runs if isinstance(item, dict)}
    aborted = evaluation.get("status", config.get("status")) == "infrastructure_aborted"
    rows = []
    for alias in ALIASES:
        cards = []
        for task in TASKS:
            run = by_task.get((alias, task), {})
            if not run:
                metadata, _ = load_object(root / "runs" / f"{alias}_{task}" / "run.json")
                run = {"status": metadata.get("status", "missing")}
            cards.append(card(root, alias, task, run, models.get(alias, "unknown"), error, aborted))
        rows.append(f'<section class="model-row" aria-label="{alias.title()}">'
                    f'<div class="model-label"><h2>{alias.title()}</h2><small>HOUSE / ZOMBIE</small></div>'
                    + "".join(cards) + '</section>')
    buttons = "".join(f'<button type="button" data-view="{key}" aria-pressed="{str(key == "beauty").lower()}">{label}</button>'
                      for key, (label, *_rest) in VIEWS.items())
    notice = '<div class="notice">整轮因基础设施问题中止；图片仅供诊断，不纳入模型比较。</div>' if aborted else ""
    if error:
        notice += f'<div class="notice">evaluation.json：{escape(error)}。评估完成后重新生成此页面。</div>'
    baseline = evaluation.get("baseline_commit", config.get("baseline_commit", "unknown"))
    effort = evaluation.get("effort", config.get("effort", "unknown"))
    seed = evaluation.get("seed", config.get("seed", "unknown"))
    evaluated_at = evaluation.get("created_at_utc", "尚未评估")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blender3DTile · 四模型资产对照</title><style>{STYLE}</style></head>
<body><main><header><div class="eyebrow">BLENDER3DTILE / ISOLATED ASSET RUNS</div>
<h1>四模型 · 房屋与僵尸</h1>
<p>同一组件工具、任务与种子的独立制作结果。每行对应一个请求模型，左右分别为房屋和僵尸。默认统一使用三分之四视角。</p>
<p class="snapshot">基线 {escape(str(baseline))} · effort {escape(str(effort))} · seed {escape(str(seed))}<br>评估快照 {escape(str(evaluated_at))}</p>
{notice}</header><nav class="toolbar" aria-label="统一预览模式"><span class="toolbar-label">切换全部视图</span>{buttons}</nav>
<noscript>启用 JavaScript 可切换白模、UV 与抬臂视图；默认外观图及下载链接仍可使用。</noscript>
{''.join(rows)}<footer>
<p>每个模型每项任务仅一次运行。验收状态描述技术合同，不是美术排名，也不证明模型正确理解了图片。</p>
<p>抬臂模式统一显示角色正面 120° 姿态，房屋保持静态外观。图片按各资产既有取景显示，不调整模型比例或重制图片。</p>
<p>这是静态离线页面，仅引用本次 run 目录中的既有 PNG / GLB；移动时请保留目录结构。生成于 {escape(generated_at)}。</p>
</footer></main><script>{SCRIPT}</script></body></html>
'''
    target = root / "gallery.html"
    target.write_text(document, encoding="utf-8")
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args(argv)
    path = generate(args.run_root)
    print(json.dumps({"gallery": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
