"""进程池可用性检查（Windows/Linux 通用）。

Windows 上 multiprocessing 默认 spawn 启动方式，要求入口受
`if __name__ == "__main__"` 保护；本脚本即按此约定编写。
用法: python scripts/check_pool.py
"""

import multiprocessing as mp


def _square(x):
    return x * x


def main():
    mp.freeze_support()  # Windows 打包兼容，Linux 下为空操作
    ctx = mp.get_context("spawn")  # 显式 spawn，与 Windows 默认一致
    with ctx.Pool(processes=2) as pool:
        out = pool.map(_square, [1, 2, 3, 4])
    assert out == [1, 4, 9, 16], out
    print(f"POOL_OK result={out}")


if __name__ == "__main__":
    main()
