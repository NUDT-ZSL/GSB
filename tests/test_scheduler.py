import unittest

from dataclasses import replace

from dungeon_render.models import TileRect
from dungeon_render.scheduler import InFlightGate, execute_plan, plan_rounds
from tests.test_validation import make_level, make_task
from dungeon_render.validation import parse_level, parse_task


def rect(index):
    # Keep col/row consistent with the task's 4-column tile grid so the
    # canonical row-major tile index equals the list position.
    row, col = divmod(index, 4)
    return TileRect(col, row, col * 10, row * 10, 10, 10)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        level = parse_level(make_level())
        # Scheduler math is independent of level geometry; override the tile
        # grid after schema validation to a 4x2 layout matching rect().
        self.task = replace(
            parse_task(make_task(), level), tile_cols=4, tile_rows=2
        )
        self.tiles = [rect(i) for i in range(8)]

    def test_round_budget_drives_admission(self):
        task = replace(self.task, round_budget=250)
        rounds = plan_rounds(task, self.tiles)
        # Each tile costs 100; at most two fit before the 250 budget yields.
        self.assertEqual(
            [r.tile_indices for r in rounds],
            [(0, 1), (2, 3), (4, 5), (6, 7)],
        )
        self.assertEqual([r.logical_cost for r in rounds], [200, 200, 200, 200])

    def test_plan_independent_of_concurrency(self):
        baseline = plan_rounds(self.task, self.tiles)
        for concurrency in (1, 2, 16):
            task = replace(self.task, concurrency=concurrency)
            self.assertEqual(plan_rounds(task, self.tiles), baseline)

    def test_execution_processes_every_tile_once_in_order(self):
        completed = []
        rounds = plan_rounds(self.task, self.tiles)
        processed = execute_plan(
            self.task,
            self.tiles,
            rounds,
            worker=lambda tile: tile.col + tile.row * 4,
            on_complete=lambda index, result: completed.append((index, result)),
        )
        flat = [index for round_tiles in processed for index in round_tiles]
        self.assertEqual(flat, list(range(8)))

    def test_in_flight_pixel_gate_blocks_over_limit(self):
        gate = InFlightGate(concurrency=4, pixel_limit=250)
        self.assertTrue(gate.try_acquire(100))
        self.assertTrue(gate.try_acquire(100))
        self.assertFalse(gate.try_acquire(100))
        self.assertEqual(gate.in_flight_pixels, 200)
        gate.release(100)
        self.assertTrue(gate.try_acquire(100))
        gate.release(200)
        self.assertEqual(gate.in_flight_pixels, 0)

    def test_gate_respects_concurrency_limit(self):
        gate = InFlightGate(concurrency=1, pixel_limit=1000)
        self.assertTrue(gate.try_acquire(10))
        self.assertFalse(gate.try_acquire(10))
        gate.release(10)
        self.assertTrue(gate.try_acquire(10))
        gate.release(10)

    def test_stop_event_only_takes_effect_at_tile_boundary(self):
        from dungeon_render.errors import GracefulInterrupt

        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 3

        with self.assertRaises(GracefulInterrupt):
            execute_plan(
                self.task,
                self.tiles,
                plan_rounds(self.task, self.tiles),
                worker=lambda tile: 0,
                on_complete=lambda index, result: None,
                stop_event=stop,
            )


if __name__ == "__main__":
    unittest.main()
