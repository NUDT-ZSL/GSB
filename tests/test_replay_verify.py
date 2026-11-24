"""帧采纳裁决、确定性重放与重放校验偏差定位测试."""

from castdiag.adjudicate import ADOPT, LATE_DROP, ROLLBACK, decide, zero_state
from castdiag.errors import E_REPLAY
from castdiag.replay import replay_events, sort_events
from castdiag.verify import build_initial_states, verify_replay


def _ev(seq, screen, gen, frame_no, sample_ms, keyframe=False):
    return {"seq": seq, "session_id": "s", "screen_id": screen, "generation": gen,
            "frame_no": frame_no, "sample_ms": sample_ms, "payload_hash": "x",
            "codec": "h264", "keyframe": keyframe}


def test_late_drop_within_tolerance_window():
    st = zero_state("tv", 2, 500)
    assert decide(st, _ev(0, "tv", 2, 1, 3000)) == ADOPT
    # 采样时刻 < 播放位置 - 容忍窗口 => 迟到丢弃
    assert decide(st, _ev(1, "tv", 2, 2, 1000)) == LATE_DROP
    assert st.position_ms == 3000  # 状态未被迟到帧污染
    # 窗口内(3000-500=2500 之后)仍采纳
    assert decide(st, _ev(2, "tv", 2, 3, 2600)) == ADOPT


def test_rollback_rebuilds_from_last_keyframe():
    st = zero_state("tv", 2, 500)
    decide(st, _ev(0, "tv", 2, 1, 0, keyframe=True))
    decide(st, _ev(1, "tv", 2, 31, 990, keyframe=True))
    decide(st, _ev(2, "tv", 2, 40, 1290))
    assert st.keyframe_seq == 31 and st.last_keyframe_ms == 990
    # 旧代数帧到达 => 代数回退, 重建自最近关键帧
    assert decide(st, _ev(3, "tv", 1, 35, 1100)) == ROLLBACK
    assert st.generation == 1
    assert st.decoded_frame_no == 35  # 回退后采纳该帧
    assert st.keyframe_seq == 31      # 关键帧基线保持


def test_keyframe_resets_decode_baseline():
    st = zero_state("tv", 2, 500)
    decide(st, _ev(0, "tv", 2, 10, 300))
    decide(st, _ev(1, "tv", 2, 20, 600, keyframe=True))
    assert st.keyframe_seq == 20 and st.last_keyframe_ms == 600
    assert st.decoded_frame_no == 20


def test_replay_determinism(sample_dir):
    from castdiag.pipeline import prepare
    from castdiag.validate import load_events

    prep = prepare(sample_dir)
    events = sort_events(load_events(sample_dir / "frames.jsonl"))
    initial = build_initial_states(prep["view"])
    states1, trace1, counts1 = replay_events(events, initial)
    states2, trace2, counts2 = replay_events(events, initial)
    assert {k: v.to_dict() for k, v in states1.items()} == \
           {k: v.to_dict() for k, v in states2.items()}
    assert trace1 == trace2 and counts1 == counts2
    assert counts1["late_drop"] == 2 and counts1["rollback"] == 1


def test_verify_pass_on_generated_sample(sample_dir):
    from castdiag.pipeline import prepare
    from castdiag.validate import load_events

    prep = prepare(sample_dir)
    report = verify_replay(prep["view"], load_events(sample_dir / "frames.jsonl"))
    assert report["ok"] is True
    assert report["stats"]["screens_compared"] == 2
    assert report["stats"]["screens_tombstoned"] == 1


def test_verify_locates_first_divergence(sample_dir):
    from castdiag.pipeline import prepare
    from castdiag.validate import load_events

    prep = prepare(sample_dir)
    events = load_events(sample_dir / "frames.jsonl")
    # 篡改: 删除 living-tv 的最后一个关键帧事件 => 关键帧序号与播放位置偏差
    victim = max(
        (e for e in events if e["screen_id"] == "living-tv" and e["keyframe"]),
        key=lambda e: e["sample_ms"],
    )
    tampered = [e for e in events if e["seq"] != victim["seq"]]
    report = verify_replay(prep["view"], tampered)
    assert report["ok"] is False
    assert report["code"] == E_REPLAY
    first = report["divergences"][0]
    assert first["screen_id"] == "living-tv"
    assert first["field"] in ("keyframe_seq", "position_ms", "generation")
    assert first["first_divergent_ms"] is not None
    assert first["event_seq"] is not None
