from __future__ import annotations

from formbench.detect import detect_blocks
from formbench.evaluation import iou_bbox, match_to_truth
from formbench.imaging import read_pgm
from formbench.runner import RunOptions, Workbench, serial_recognize
from formbench.schema import load_truth


NORMAL_IDS = ["form-001", "form-002", "form-003"]


def _spec(task, image_id):
    return next(spec for spec in task.images if spec.image_id == image_id)


def test_detection_is_deterministic_sorted_and_truth_aligned(task):
    for image_id in NORMAL_IDS:
        spec = _spec(task, image_id)
        image = read_pgm(spec.path)
        first = [block.to_dict() for block in detect_blocks(image, detection_threshold=task.detection_threshold)]
        second = [block.to_dict() for block in detect_blocks(image, detection_threshold=task.detection_threshold)]
        truth = load_truth(spec.truth_path)

        assert first == second
        keys = [(item["bbox"][0], item["bbox"][1], item["bbox"][2], item["bbox"][3]) for item in first]
        assert keys == sorted(keys)
        assert len(first) == len(truth) == 11

        blocks = detect_blocks(image, detection_threshold=task.detection_threshold)
        mapping = match_to_truth(blocks, truth)
        ious = [mapping[index][1] for index in range(len(blocks))]
        assert min(ious) >= 0.70
        assert sum(ious) / len(ious) >= 0.85


def test_detection_keeps_block_crossing_chunk_boundary_whole(task):
    spec = _spec(task, "form-001")
    image = read_pgm(spec.path)
    blocks = detect_blocks(image, detection_threshold=task.detection_threshold)
    crossing = [
        block
        for block in blocks
        if block.x < task.image_width // 2 < block.x + block.width
    ]
    assert crossing
    assert any(block.width > 250 for block in crossing)


def test_parallel_results_match_serial_exactly(task, tmp_path):
    spec = _spec(task, "form-001")
    serial = serial_recognize(task, spec)

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    with Workbench(
        task,
        options=RunOptions(state_dir=state_dir, output_dir=output_dir, concurrency=4),
    ) as bench:
        outcome = bench.process_image(spec)

    parallel = outcome["result"]["blocks"]
    assert len(parallel) == len(serial)
    for parallel_block, serial_block in zip(
        sorted(parallel, key=lambda item: item["block_index"]), serial
    ):
        assert parallel_block["text"] == serial_block["text"]
        assert parallel_block["confidence"] == serial_block["confidence"]
        assert parallel_block["score"] == serial_block["score"]
        assert parallel_block["bbox"] == serial_block["bbox"]
    assert outcome["result"]["adjudication"]["passed"] is True


def test_concurrency_one_and_four_produce_same_blocks(task, tmp_path):
    spec = _spec(task, "form-002")
    outputs = []
    for concurrency in (1, 4):
        with Workbench(
            task,
            options=RunOptions(
                state_dir=tmp_path / f"state-{concurrency}",
                output_dir=tmp_path / f"output-{concurrency}",
                concurrency=concurrency,
            ),
        ) as bench:
            outcome = bench.process_image(spec)
        outputs.append(outcome["result"]["blocks"])

    one, four = outputs
    assert [item["text"] for item in one] == [item["text"] for item in four]
    assert [item["confidence"] for item in one] == [item["confidence"] for item in four]
    assert [item["content_digest"] for item in one] == [
        item["content_digest"] for item in four
    ]
