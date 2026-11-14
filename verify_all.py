# -*- coding: utf-8 -*-
"""
verify_all.py
=============
对 frame_anim_tool.py 的 15 条验收项做本地可复现自动化验证。
所有夹具在系统临时目录中现建（含损坏 PNG、缺文件、超尺寸、全透明等），
通过子进程以 `python -I` 调用命令行，再用 Pillow 读回 GIF/PNG 断言结果。

用法：
  python -I verify_all.py
退出码：0 全部通过；1 存在失败项。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK 代码页，统一重配为 UTF-8，避免中文输出乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from PIL import Image

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "frame_anim_tool.py"
SAMPLE_GEN = ROOT / "make_samples.py"

WORKSPACE = Path(tempfile.mkdtemp(prefix="fat_verify_"))
results: list[tuple[str, str, bool, str]] = []


# --------------------------------------------------------------------------- 夹具辅助
def solid(path: Path, size, rgb, alpha: int = 255) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, rgb + (alpha,)).save(path)


def transparent(path: Path, size) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, (0, 0, 0, 0)).save(path)


def edge_frame(path: Path) -> None:
    """透明画布上的方块：中心不透明(alpha=255)、外圈半透明(alpha=128)、四周全透明(alpha=0)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, 31, 31], fill=(255, 0, 0, 128))   # 半透明外圈
    d.rectangle([12, 12, 27, 27], fill=(255, 0, 0, 255))  # 不透明中心
    img.save(path)


def write_manifest(case_dir: Path, obj: dict, name: str = "manifest.json") -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    p = case_dir / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def base_manifest(frames, loop=2, canvas=(64, 64), bg="#0066cc"):
    return {
        "canvas": {"width": canvas[0], "height": canvas[1]},
        "loop_count": loop,
        "background_color": bg,
        "frames": frames,
    }


def run_cli(manifest: Path, outdir: Path):
    return subprocess.run(
        [sys.executable, "-I", str(TOOL), "--manifest", str(manifest), "--output-dir", str(outdir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def load_report(outdir: Path) -> dict:
    return json.loads((outdir / "report.json").read_text(encoding="utf-8"))


def read_gif(gif_path: Path):
    with Image.open(gif_path) as im:
        n = getattr(im, "n_frames", 1)
        loop = im.info.get("loop")
        durations, centers = [], []
        for i in range(n):
            im.seek(i)
            durations.append(im.info.get("duration"))
            centers.append(im.convert("RGB").getpixel((im.width // 2, im.height // 2)))
        size = im.size
    return {"n_frames": n, "loop": loop, "durations": durations, "centers": centers, "size": size}


def record(req: str, name: str, ok: bool, detail: str = "") -> None:
    results.append((req, name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {req} {name} {('- ' + detail) if detail else ''}")


def case_dir(name: str) -> Path:
    d = WORKSPACE / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- 验收用例
def t01_canvas_validation():
    """缺画布尺寸 -> 非零退出并指出字段名。"""
    d = case_dir("t01")
    # 1a 整个 canvas 缺失
    m1 = {"loop_count": 1, "frames": []}
    p1 = run_cli(write_manifest(d / "a", m1), d / "out_a")
    ok1 = p1.returncode == 2 and "canvas" in p1.stderr
    # 1b 只缺 width
    m2 = {"canvas": {"height": 64}, "frames": []}
    p2 = run_cli(write_manifest(d / "b", m2), d / "out_b")
    ok2 = p2.returncode == 2 and "canvas.width" in p2.stderr
    # 1c 类型错误（width 为字符串）
    m3 = {"canvas": {"width": "64", "height": 64}, "frames": []}
    p3 = run_cli(write_manifest(d / "c", m3), d / "out_c")
    ok3 = p3.returncode == 2 and "canvas.width" in p3.stderr
    record("需求1", "画布字段缺失/类型错误报具体字段且非零退出", ok1 and ok2 and ok3,
           f"rc=({p1.returncode},{p2.returncode},{p3.returncode})")


def t02_bad_frames_isolated():
    """超尺寸 + 损坏混入，其余有效帧全部合成。"""
    d = case_dir("t02")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    solid(d / "big.png", (72, 64), (255, 0, 255))
    (d / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    solid(d / "b.png", (64, 64), (0, 204, 0))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "big.png", "delay_ms": 100},
        {"file": "broken.png", "delay_ms": 100},
        {"file": "b.png", "delay_ms": 100},
    ])
    out = d / "out"
    p = run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    gif = read_gif(out / "preview.gif")
    codes = {f["sequence"]: f["reason_code"] for f in rep["frames"] if not f["valid"]}
    ok = (p.returncode == 0 and rep["summary"]["valid_frames"] == 2
          and codes.get(2) == "oversized" and codes.get(3) == "corrupt_image"
          and gif["n_frames"] == 2)
    record("需求2", "超尺寸/损坏帧不影响其余帧合成", ok,
           f"valid={rep['summary']['valid_frames']}, gif帧={gif['n_frames']}, 原因={codes}")


def t03_delay_zero_invalid():
    """延时 0 判无效且报告说明原因，其余帧正常。"""
    d = case_dir("t03")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    solid(d / "z.png", (64, 64), (0, 0, 255))
    solid(d / "b.png", (64, 64), (0, 204, 0))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "z.png", "delay_ms": 0},
        {"file": "b.png", "delay_ms": 200},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    bad = [f for f in rep["frames"] if not f["valid"]]
    ok = (len(bad) == 1 and bad[0]["sequence"] == 2 and bad[0]["reason"] == "延时非法"
          and rep["summary"]["valid_frames"] == 2 and "越界" in bad[0]["detail"])
    record("需求3", "延时0判无效并说明原因", ok, f"detail={bad[0]['detail'] if bad else 'N/A'}")


def t04_total_duration():
    """100/200/300 三帧 -> 总时长 600ms，逐帧延时保持。"""
    d = case_dir("t04")
    for n, c in [("a", (255, 0, 0)), ("b", (0, 204, 0)), ("c", (0, 0, 255))]:
        solid(d / f"{n}.png", (64, 64), c)
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "b.png", "delay_ms": 200},
        {"file": "c.png", "delay_ms": 300},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    gif = read_gif(out / "preview.gif")
    ok = (rep["summary"]["total_duration_ms"] == 600
          and sum(gif["durations"]) == 600 and gif["durations"] == [100, 200, 300])
    record("需求4", "GIF逐帧延时保持且总时长=600ms", ok,
           f"report={rep['summary']['total_duration_ms']}, readback={gif['durations']}")


def t05_loop_count():
    """循环2次可读回；缺省写无限并在报告注明来源。"""
    d = case_dir("t05")
    frames = [{"file": "a.png", "delay_ms": 100}]
    m1_dir, m2_dir = d / "m1", d / "m2"
    # 帧文件按清单所在目录的相对路径解析，因此帧与清单放在同一目录
    solid(m1_dir / "a.png", (64, 64), (255, 0, 0))
    out1 = d / "out_loop2"
    run_cli(write_manifest(m1_dir, base_manifest(frames, loop=2)), out1)
    gif1 = read_gif(out1 / "preview.gif")
    rep1 = load_report(out1)

    m_default = base_manifest(frames)
    del m_default["loop_count"]
    out2 = d / "out_default"
    solid(m2_dir / "a.png", (64, 64), (255, 0, 0))
    run_cli(write_manifest(m2_dir, m_default), out2)
    gif2 = read_gif(out2 / "preview.gif")
    rep2 = load_report(out2)
    ok = (gif1["loop"] == 2 and rep1["loop_count"] == 2
          and gif2["loop"] == 0 and rep2["loop_count"] == 0
          and rep2["loop_count_source"] == "default_infinite" and "无限" in rep2["loop_note"])
    record("需求5", "循环次数写GIF/缺省无限并注明来源", ok,
           f"loop2读回={gif1['loop']}, 缺省读回={gif2['loop']}, 来源={rep2['loop_count_source']}")


def t06_alpha_preview_no_black():
    """半透明边缘的透明通道预览：边缘无纯黑像素且灰阶可辨。"""
    d = case_dir("t06")
    edge_frame(d / "edge.png")
    man = base_manifest([{"file": "edge.png", "delay_ms": 100}])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    alpha_name = rep["frames"][0]["alpha_preview"]
    prev = Image.open(out / alpha_name)
    orig = Image.open(d / "edge.png").convert("RGBA")
    a_data = list(orig.getchannel("A").get_flattened_data())
    p_data = list(prev.get_flattened_data())
    semi = [(a, v) for a, v in zip(a_data, p_data) if 0 < a < 255]
    preview_values = set(p_data)
    no_black_on_edge = all(1 <= v <= 254 for _, v in semi)            # 半透明处绝不为纯黑
    mapping_ok = all(v == 255 - a for a, v in semi)                   # 灰阶映射正确
    # 三档可辨：不透明->0(纯黑)、半透明->127(灰)、全透明->255(纯白)
    distinguishable = {0, 127, 255}.issubset(preview_values)
    ok = prev.mode == "L" and no_black_on_edge and mapping_ok and distinguishable
    record("需求6", "透明通道预览半透明边缘无纯黑且可辨", ok,
           f"mode={prev.mode}, 半透明像素数={len(semi)}, 边缘取值样例={sorted({v for _, v in semi})}")


def t07_fully_transparent():
    """全透明帧单列、不计总时长。"""
    d = case_dir("t07")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    transparent(d / "empty.png", (64, 64))
    solid(d / "b.png", (64, 64), (0, 204, 0))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "empty.png", "delay_ms": 500},
        {"file": "b.png", "delay_ms": 200},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    gif = read_gif(out / "preview.gif")
    bad = [f for f in rep["frames"] if not f["valid"]]
    ok = (len(bad) == 1 and bad[0]["reason_code"] == "fully_transparent"
          and rep["summary"]["total_duration_ms"] == 300 and gif["n_frames"] == 2)
    record("需求7", "全透明帧判空帧且不计入总时长", ok,
           f"总时长={rep['summary']['total_duration_ms']}ms(不含500ms空帧)")


def t08_centering():
    """小图居中、边距相等、背景填充与清单一致。"""
    d = case_dir("t08")
    solid(d / "small.png", (32, 24), (255, 204, 0))
    man = base_manifest([{"file": "small.png", "delay_ms": 100}])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    gif = read_gif(out / "preview.gif")
    pl = rep["frames"][0]["placement"]
    with Image.open(out / "preview.gif") as im:
        im.seek(0)
        rgb = im.convert("RGB")
        corner = rgb.getpixel((0, 0))
        center = rgb.getpixel((32, 32))
    ok = (pl["margin_left"] == pl["margin_right"] == 16
          and pl["margin_top"] == pl["margin_bottom"] == 20
          and corner == (0, 102, 204) and center == (255, 204, 0)
          and gif["size"] == (64, 64))
    record("需求8", "小图居中、等边距、背景色填充", ok,
           f"margins=L{pl['margin_left']}/R{pl['margin_right']}/T{pl['margin_top']}/B{pl['margin_bottom']}, "
           f"角={corner}, 中心={center}")


def t09_missing_file():
    """删除中间帧：报告按序号定位，前后帧仍合成。"""
    d = case_dir("t09")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    solid(d / "b.png", (64, 64), (0, 204, 0))
    solid(d / "c.png", (64, 64), (0, 0, 255))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "gone.png", "delay_ms": 100},
        {"file": "b.png", "delay_ms": 100},
        {"file": "c.png", "delay_ms": 100},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    gif = read_gif(out / "preview.gif")
    bad = [f for f in rep["frames"] if not f["valid"]]
    ok = (len(bad) == 1 and bad[0]["sequence"] == 2 and bad[0]["reason_code"] == "missing_file"
          and "gone.png" in bad[0]["detail"] and gif["n_frames"] == 3)
    record("需求9", "缺文件按帧序号定位且不中断", ok,
           f"缺失序号={bad[0]['sequence'] if bad else 'N/A'}, GIF帧={gif['n_frames']}")


def t10_counts_sum():
    """有效+无效=清单条目总数。"""
    d = case_dir("t10")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    transparent(d / "e.png", (64, 64))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "e.png", "delay_ms": 100},
        {"file": "nope.png", "delay_ms": 100},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    s = load_report(out)["summary"]
    ok = s["valid_frames"] + s["invalid_frames"] == s["total_entries"] == 3
    record("需求10", "有效/无效帧数之和等于条目总数", ok, f"{s['valid_frames']}+{s['invalid_frames']}={s['total_entries']}")


def t11_reason_distinction():
    """缺失与全透明等原因在报告中分别可见。"""
    d = case_dir("t11")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    transparent(d / "e.png", (64, 64))
    (d / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    solid(d / "big.png", (72, 64), (255, 0, 255))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "e.png", "delay_ms": 100},
        {"file": "missing.png", "delay_ms": 100},
        {"file": "broken.png", "delay_ms": 100},
        {"file": "big.png", "delay_ms": 100},
        {"file": "a.png", "delay_ms": 0},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    codes = sorted({f["reason_code"] for f in rep["frames"] if not f["valid"]})
    labels = sorted({f["reason"] for f in rep["frames"] if not f["valid"]})
    expected = {"fully_transparent", "missing_file", "corrupt_image", "oversized", "delay_out_of_range"}
    ok = expected.issubset(set(codes))
    record("需求11", "五类未完成原因分别可见", ok, f"codes={codes}, labels={labels}")


def t12_frame_order():
    """中间插入无效帧，GIF 帧序仍与剩余有效帧一致。"""
    d = case_dir("t12")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    solid(d / "c.png", (64, 64), (0, 204, 204))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "gone.png", "delay_ms": 100},
        {"file": "c.png", "delay_ms": 100},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    gif = read_gif(out / "preview.gif")
    ok = gif["n_frames"] == 2 and gif["centers"][0] == (255, 0, 0) and gif["centers"][1] == (0, 204, 204)
    record("需求12", "无效帧不改变有效帧先后顺序", ok, f"帧中心色序={gif['centers']}")


def t13_bounds():
    """延时超上界/低下界判无效并写明越界原因；画布越界清单级报错。"""
    d = case_dir("t13")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    solid(d / "hi.png", (64, 64), (0, 0, 255))
    solid(d / "lo.png", (64, 64), (0, 204, 0))
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "hi.png", "delay_ms": 99999},
        {"file": "lo.png", "delay_ms": 5},
    ])
    out = d / "out"
    run_cli(write_manifest(d, man), out)
    rep = load_report(out)
    bad = {f["sequence"]: f for f in rep["frames"] if not f["valid"]}
    ok = (set(bad) == {2, 3} and all(f["reason_code"] == "delay_out_of_range" for f in bad.values())
          and all("越界" in f["detail"] for f in bad.values()))
    # 画布越界 -> 清单级非零退出
    bad_canvas = base_manifest([], canvas=(99999, 8))
    p = run_cli(write_manifest(d / "mc", bad_canvas), d / "out_c")
    ok = ok and p.returncode == 2 and "canvas.width" in p.stderr and "越界" in p.stderr
    record("需求13", "延时/画布越界判无效并写明越界", ok,
           f"延时细节={[bad[k]['detail'] for k in sorted(bad)]}; 画布rc={p.returncode}")


def t14_deterministic():
    """重复执行，GIF 与报告逐字节相同。"""
    d = case_dir("t14")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    edge_frame(d / "e.png")
    man = base_manifest([
        {"file": "a.png", "delay_ms": 100},
        {"file": "e.png", "delay_ms": 250},
        {"file": "missing.png", "delay_ms": 100},
    ])
    mp = write_manifest(d, man)
    o1, o2 = d / "run1", d / "run2"
    r1 = run_cli(mp, o1)
    r2 = run_cli(mp, o2)

    def digest(p: Path):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    g1, g2 = digest(o1 / "preview.gif"), digest(o2 / "preview.gif")
    j1, j2 = digest(o1 / "report.json"), digest(o2 / "report.json")
    a1 = digest(o1 / "alpha_preview" / "frame_001_alpha.png")
    a2 = digest(o2 / "alpha_preview" / "frame_001_alpha.png")
    ok = r1.returncode == 0 and r2.returncode == 0 and g1 == g2 and j1 == j2 and a1 == a2
    record("需求14", "重复运行产物逐字节一致", ok, f"gif={g1[:12]}…, report={j1[:12]}…, alpha={a1[:12]}…")


def t15_output_dir_creation():
    """指定不存在的嵌套输出目录，自动创建并生成全部产物。"""
    d = case_dir("t15")
    solid(d / "a.png", (64, 64), (255, 0, 0))
    man = base_manifest([{"file": "a.png", "delay_ms": 100}])
    out = d / "nested" / "deep" / "output"
    p = run_cli(write_manifest(d, man), out)
    ok = (p.returncode == 0 and out.is_dir() and (out / "preview.gif").is_file()
          and (out / "report.json").is_file()
          and (out / "alpha_preview" / "frame_000_alpha.png").is_file())
    record("需求15", "输出目录自动创建且产物齐全", ok, f"目录={out}")


def integration_demo_samples():
    """用 make_samples.py 的完整样例跑一次端到端集成检查。"""
    d = case_dir("integration")
    p_gen = subprocess.run([sys.executable, "-I", str(SAMPLE_GEN), "--out", str(d / "samples")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = d / "samples_output"
    p_run = run_cli(d / "samples" / "manifest.json", out)
    rep = load_report(out)
    gif = read_gif(out / "preview.gif")
    s = rep["summary"]
    ok = (p_gen.returncode == 0 and p_run.returncode == 0
          and s["total_entries"] == 12 and s["valid_frames"] == 6 and s["invalid_frames"] == 6
          and s["total_duration_ms"] == 900 and gif["n_frames"] == 6 and gif["loop"] == 2
          and gif["durations"] == [100, 200, 300, 100, 100, 100])
    record("集成", "make_samples 全套样例端到端", ok,
           f"有效{s['valid_frames']}/无效{s['invalid_frames']}, 总时长{s['total_duration_ms']}ms, "
           f"GIF延时={gif['durations']}")


def main() -> int:
    print(f"工作区（夹具与产物可复查）：{WORKSPACE}\n")
    tests = [
        t01_canvas_validation, t02_bad_frames_isolated, t03_delay_zero_invalid,
        t04_total_duration, t05_loop_count, t06_alpha_preview_no_black,
        t07_fully_transparent, t08_centering, t09_missing_file, t10_counts_sum,
        t11_reason_distinction, t12_frame_order, t13_bounds, t14_deterministic,
        t15_output_dir_creation, integration_demo_samples,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # 单条用例异常不应中断其余验收
            record(t.__name__, "用例异常", False, f"{type(e).__name__}: {e}")

    passed = sum(1 for r in results if r[2])
    print("\n================ 汇总 ================")
    for req, name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {req:<4} {name}")
    print(f"\n通过 {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
