import threading
import time

from matgen.engine import GenerationEngine, MemoryBudget, estimate_tile_bytes
from matgen.tiling import plan_chunks
from tests.helpers import make_config, make_spec


def test_memory_budget_blocks_then_releases_in_order():
    budget = MemoryBudget(100)
    order = []

    def worker(need: int, name: str):
        budget.acquire(need)
        order.append(name)
        time.sleep(0.01)
        budget.release(need)

    threads = [threading.Thread(target=worker, args=(60, f"t{i}")) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert order == ["t0", "t1", "t2"]


def test_tiny_budget_does_not_deadlock_and_keeps_precision(tmp_path):
    state, output, archive = tmp_path / "state", tmp_path / "output", tmp_path / "archive"
    spec = make_spec(width=48, height=48, grid_rows=3, grid_cols=3)
    smallest = min(estimate_tile_bytes(plan) for plan in plan_chunks(spec))
    config = make_config(
        concurrency=4,
        memory_budget_bytes=1,  # 远小于任何单块，验证单块超预算时不死锁
        state_dir=str(state),
        output_dir=str(output),
        archive_dir=str(archive),
    )
    result = GenerationEngine(config).run_material(spec, state, output, archive)
    assert result.status == "completed"
    assert result.tile_count == len(plan_chunks(spec))
    assert result.consistency["passed"] is True
    assert smallest > 1
