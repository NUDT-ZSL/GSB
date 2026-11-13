"""确定性样例生成启动器。

用法：
  python -I generate_samples.py [输出目录]
不指定目录时使用系统临时目录下的 pysentry_samples。
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pysentry.samples import build_sample_tree  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    target = (
        pathlib.Path(sys.argv[1])
        if len(sys.argv) > 1
        else pathlib.Path(tempfile.gettempdir()) / "pysentry_samples"
    )
    build_sample_tree(target)
    print(f"样例已生成到: {target}")
    for sub in sorted(p.name for p in target.iterdir() if p.is_dir()):
        print(f"  - {target / sub}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
