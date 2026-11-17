"""并行解码/脉冲检测调度：并发上限 + 在途内存预算双约束。

调度确定性：
* 任务严格按设备段声明顺序提交，预算不足时靠后的段等待，
  绝不截短搜索窗或降低检测精度；
* 任一段失败立即取消所有待处理段，已完成合法段结果保留；
* 最终结果按声明顺序归并，与并发度和内存预算无关。
"""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, ALL_COMPLETED

from .errors import BudgetError, FormatError
from .wavio import decode_wav, read_header
from .sync import detect_peaks, estimate_clock


def segment_memory_need(info):
    """在途峰值占用：原始 PCM 字节 + 解码 float64 采样 + 检测能量视图。"""
    decoded_bytes = info.frames * info.channels * 8
    return info.data_bytes + 2 * decoded_bytes


def _process_segment(path, pulses, peak_threshold, dev_rate, ref_rate,
                     model_order, refs):
    info, samples = decode_wav(path)
    observations = detect_peaks(samples, info.sample_rate, pulses,
                                peak_threshold)
    clock = estimate_clock(observations, info.sample_rate, ref_rate,
                           model_order, refs)
    return {
        "wav_info": info.as_dict(),
        "observations": observations,
        "clock": clock,
        "samples": samples,
    }


def run_segment_jobs(session, ref_rate, store, signaler, reuse):
    """返回按声明顺序排列的段处理结果；单段超预算直接 E_BUDGET。"""
    planned = list(session.iter_segments())
    needs = []
    for dev, seg in planned:
        info = read_header(session.resolve(seg.file))
        if info.channels != len(seg.channels):
            raise FormatError(
                "WAV 声道数与声明布局不一致",
                refs=[dev.device_id, seg.segment_id],
                detail={"wav_channels": info.channels,
                        "declared_channels": len(seg.channels)})
        need = segment_memory_need(info)
        if need > session.memory_budget_bytes:
            raise BudgetError(
                "单段内存需求超过会话预算，无法在不降低精度的前提下处理",
                refs=[dev.device_id, seg.segment_id],
                detail={"need_bytes": need,
                        "budget_bytes": session.memory_budget_bytes})
        needs.append(need)

    results = [None] * len(planned)
    in_flight = {}
    next_submit = 0
    used_bytes = 0
    errors = []
    acknowledged = set()
    executor = ThreadPoolExecutor(max_workers=session.concurrency)
    active_slots = 0
    try:
        while not errors and (next_submit < len(planned) or in_flight):
            while next_submit < len(planned):
                idx = next_submit
                dev, seg = planned[idx]
                need = needs[idx]
                if (active_slots >= session.concurrency or
                        used_bytes + need > session.memory_budget_bytes):
                    break
                cached = reuse.get((dev.device_id, seg.segment_id))
                if cached is not None:
                    info, samples = decode_wav(session.resolve(seg.file))
                    results[idx] = {
                        "wav_info": info.as_dict(),
                        "observations": cached["observations"],
                        "clock": cached["clock"],
                        "samples": samples,
                        "restored": True,
                    }
                    active_slots += 1
                    used_bytes += need
                    active_slots -= 1
                    used_bytes -= need
                    next_submit += 1
                    signaler.check(store, "segment", idx)
                    continue
                future = executor.submit(
                    _process_segment, session.resolve(seg.file), seg.pulses,
                    session.peak_threshold, dev.sample_rate, ref_rate,
                    session.model_order,
                    {"device_id": dev.device_id, "segment_id": seg.segment_id})
                in_flight[future] = (idx, need, dev, seg)
                used_bytes += need
                active_slots += 1
                next_submit += 1

            if not in_flight:
                continue
            done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                idx, need, dev, seg = in_flight.pop(future)
                used_bytes -= need
                active_slots -= 1
                try:
                    result = future.result()
                except Exception as exc:  # 业务异常原样上抛，其余包装
                    errors.append((idx, exc))
                    break
                results[idx] = result
                acknowledged.add(idx)
                store.save_segment(seg, {
                    "wav_info": result["wav_info"],
                    "observations": result["observations"],
                    "clock": result["clock"],
                })
                signaler.check(store, "segment", idx)
            if errors:
                # 已在途任务不截短：等待其结束，合法结果照常持久化保留
                if in_flight:
                    done_all, _ = wait(list(in_flight.keys()),
                                       return_when=ALL_COMPLETED)
                    for future in done_all:
                        idx, _need, _dev, seg = in_flight[future]
                        try:
                            result = future.result()
                        except Exception:
                            continue
                        results[idx] = result
                        acknowledged.add(idx)
                        store.save_segment(seg, {
                            "wav_info": result["wav_info"],
                            "observations": result["observations"],
                            "clock": result["clock"],
                        })
                for future in in_flight:
                    future.cancel()
                raise errors[0][1]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return planned, results

