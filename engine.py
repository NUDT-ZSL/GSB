#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py -- 流式 24bit 小端裸 PCM 多轨混音引擎(无 WAV 头,不使用 wave 模块)

子命令:
  timeline   有理数时间轴(JSON 分段 BPM) -> 事件采样点定位(Fraction 精确,向下取整)
  positions  清单区间绝对位置(Fraction 精确值 + 单次四舍五入到整数采样点)
  mix        多轨流式混音(定点累加/线性声像/间隙填静音/重叠覆盖)
             + 两阶段峰值归一化 + 原子提交(os.replace) + 崩溃恢复日志
  copy       24bit PCM 流式读写(可配置窗口,证明内存不随文件长度增长)
  validate   按 sidecar JSON 逐块 SHA256 校验母带(支持 --crash-after-blocks 断点续算)

错误协议: 不可恢复业务错误向 stderr 输出单行 JSON {"code","message"};
退出码 0=成功, 1=业务错误, 2=使用错误。
所有时间/增益/声像/归一化计算均使用 fractions.Fraction,不引入浮点。
四舍五入一律为"半值远离零";事件定位一律向下取整。
"""

import argparse
import array
import hashlib
import json
import os
import sys
import time
from fractions import Fraction

S24_MIN = -(1 << 23)          # -8388608
S24_MAX = (1 << 23) - 1       #  8388607
VALID_RATES = (44100, 48000, 96000)
OUT_CHANNELS = 2              # 最终母带为立体声
BYTES_PER_FRAME = 3 * OUT_CHANNELS

E_TIMELINE = "E_TIMELINE"
E_SAMPLE_RANGE = "E_SAMPLE_RANGE"
E_MISSING_INPUT = "E_MISSING_INPUT"
E_LOG_CORRUPT = "E_LOG_CORRUPT"
E_COMMIT_FAILED = "E_COMMIT_FAILED"
E_EVENT_MISMATCH = "E_EVENT_MISMATCH"
E_USAGE = "E_USAGE"


class EngineError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class CommitFailed(Exception):
    """受控(可捕获)的输出提交失败:清理本轮临时文件与恢复日志,目标保持不变。"""


class LogCorrupt(Exception):
    """恢复日志缺失/损坏/偏移非法:作为可恢复告警,从头重算。"""


def emit_error(code, message):
    sys.stderr.write(json.dumps({"code": code, "message": message}) + "\n")
    sys.stderr.flush()


def silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- 精确有理数

def parse_frac(v, what="value", code=E_TIMELINE):
    """从 JSON 值解析 Fraction:接受 int、"num/den" 字符串、[num, den]。"""
    if isinstance(v, bool):
        raise EngineError(code, "invalid fraction for %s: %r" % (what, v))
    if isinstance(v, int):
        return Fraction(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            if "/" in s:
                a, b = s.split("/", 1)
                f = Fraction(int(a.strip()), int(b.strip()))
            else:
                f = Fraction(int(s))
        except (ValueError, ZeroDivisionError):
            raise EngineError(code, "invalid fraction for %s: %r" % (what, v))
        return f
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return Fraction(int(v[0]), int(v[1]))
        except (ValueError, ZeroDivisionError):
            raise EngineError(code, "invalid fraction for %s: %r" % (what, v))
    raise EngineError(code, "invalid fraction for %s: %r" % (what, v))


def floor_frac(f):
    return f.numerator // f.denominator


def rhalf_int(n, d):
    """round(n/d),半值远离零,d > 0。纯整数运算。"""
    if n >= 0:
        return (2 * n + d) // (2 * d)
    return -((-2 * n + d) // (2 * d))


def rhalf_frac(f):
    return rhalf_int(f.numerator, f.denominator)


def clamp24(v):
    if v < S24_MIN:
        return S24_MIN
    if v > S24_MAX:
        return S24_MAX
    return v


# ---------------------------------------------------------------- 24bit 小端编解码

_I32 = "i" if array.array("i").itemsize == 4 else "l"
_SIGN_TABLE = bytes(0xFF if b >= 0x80 else 0x00 for b in range(256))


def decode_s24le(buf):
    """3 字节小端有符号 -> int32 数组。原始三字节输入的所有位模式均为合法 24bit 数。"""
    if isinstance(buf, (bytearray, memoryview)):
        buf = bytes(buf)
    n = len(buf) // 3
    if sys.byteorder == "little":
        wide = bytearray(4 * n)
        wide[0::4] = buf[0::3]
        wide[1::4] = buf[1::3]
        wide[2::4] = buf[2::3]
        wide[3::4] = buf[2::3].translate(_SIGN_TABLE)
        a = array.array(_I32)
        a.frombytes(bytes(wide))
        return a
    out = array.array(_I32)
    for i in range(0, 3 * n, 3):
        v = buf[i] | (buf[i + 1] << 8) | (buf[i + 2] << 16)
        if v >= 0x800000:
            v -= 0x1000000
        out.append(v)
    return out


def encode_s24le(vals):
    """int 样本(须已在 24bit 值域内)-> 3 字节小端。"""
    if sys.byteorder == "little":
        a = vals if (isinstance(vals, array.array) and vals.typecode == _I32) \
            else array.array(_I32, vals)
        raw = a.tobytes()
        n = len(a)
        out = bytearray(3 * n)
        out[0::3] = raw[0::4]
        out[1::3] = raw[1::4]
        out[2::3] = raw[2::4]
        return bytes(out)
    out = bytearray()
    for v in vals:
        out += (v & 0xFFFFFF).to_bytes(3, "little")
    return bytes(out)


# ---------------------------------------------------------------- 时间轴

class Timeline(object):
    """分段 BPM 有理数时间轴。位置以四分音符时值(Fraction)表示。"""

    def __init__(self, segments):
        self.segments = segments  # [(Fraction start_quarters, Fraction bpm)] 已排序

    @staticmethod
    def from_json(obj):
        if not isinstance(obj, dict) or "segments" not in obj:
            raise EngineError(E_TIMELINE, "timeline JSON must contain 'segments'")
        raw = obj["segments"]
        if not isinstance(raw, list) or not raw:
            raise EngineError(E_TIMELINE, "timeline 'segments' must be a non-empty list")
        segs = []
        for i, s in enumerate(raw):
            if not isinstance(s, dict):
                raise EngineError(E_TIMELINE, "segment %d is not an object" % i)
            start = parse_frac(s.get("start", 0), "segment start")
            bpm = parse_frac(s.get("bpm"), "segment bpm")
            if bpm <= 0:
                raise EngineError(E_TIMELINE,
                                  "segment %d has non-positive BPM (%s)" % (i, bpm))
            segs.append((start, bpm))
        segs.sort(key=lambda x: x[0])
        for i in range(1, len(segs)):
            if segs[i][0] == segs[i - 1][0]:
                raise EngineError(E_TIMELINE, "duplicate segment start %s" % segs[i][0])
        events = obj.get("events", []) or []
        if not isinstance(events, list):
            raise EngineError(E_TIMELINE, "timeline 'events' must be a list")
        return Timeline(segs), events

    def samples_at(self, q, sr):
        """四分音符位置 q -> 精确 Fraction 采样位置(分段 BPM 累计,不舍入)。"""
        if q < self.segments[0][0]:
            raise EngineError(E_TIMELINE,
                              "position %s lies before first segment start" % q)
        acc = Fraction(0)
        factor = Fraction(60 * sr)
        for i, (s, b) in enumerate(self.segments):
            nxt = self.segments[i + 1][0] if i + 1 < len(self.segments) else None
            if nxt is not None and q >= nxt:
                acc += factor * (nxt - s) / b
            else:
                acc += factor * (q - s) / b
                return acc
        return acc


# ---------------------------------------------------------------- 清单

class Interval(object):
    def __init__(self):
        self.id = None
        self.start_q = Fraction(0)     # 起始四分音符位置
        self.offset = Fraction(0)      # 分数采样点偏移
        self.offset_mode = "abs"       # abs=相对全局时间轴 / delta=增量(须显式声明)
        self.length = 0                # 区间长度(帧)
        self.file_offset = 0           # 文件内起始帧
        self.constant = None           # 常量区间(确定性测试用),None 表示读文件
        self.pos_exact = Fraction(0)   # 绝对位置(Fraction 精确)
        self.start_sample = 0          # 单次四舍五入后的整数采样点
        self.decl_index = 0            # 声明顺序(同起点覆盖 tie-break)


class Track(object):
    def __init__(self):
        self.id = None
        self.path = None               # None 表示纯常量轨
        self.channels = 1
        self.gain = Fraction(1)
        self.pan = Fraction(0)
        self.intervals = []
        self.fh = None
        self.next_frame = 0            # 恢复日志用:已读到的文件帧位置


def _load_json_file(path, what):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise EngineError(E_MISSING_INPUT, "%s not found: %s" % (what, path))
    try:
        return data, json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise EngineError(E_USAGE, "%s is not valid JSON: %s" % (what, e))


def parse_tracks(mobj, mdir, timeline, sr):
    raw = mobj.get("tracks")
    if not isinstance(raw, list) or not raw:
        raise EngineError(E_USAGE, "manifest must contain a non-empty 'tracks' list")
    tracks = []
    for t in raw:
        tr = Track()
        tr.id = t.get("id")
        if not tr.id:
            raise EngineError(E_USAGE, "track missing 'id'")
        if t.get("channels") not in (1, 2):
            raise EngineError(E_USAGE, "track %s: channels must be 1 or 2" % tr.id)
        tr.channels = t["channels"]
        tr.gain = parse_frac(t.get("gain", 1), "gain", E_USAGE)
        tr.pan = parse_frac(t.get("pan", 0), "pan", E_USAGE)
        if not (Fraction(-1) <= tr.pan <= Fraction(1)):
            raise EngineError(E_USAGE, "track %s: pan out of [-1,1]" % tr.id)
        if "file" in t:
            tr.path = os.path.join(mdir, t["file"])
            if not os.path.isfile(tr.path):
                raise EngineError(E_MISSING_INPUT, "track file not found: %s" % tr.path)
        raw_ivs = t.get("intervals", [])
        if not isinstance(raw_ivs, list):
            raise EngineError(E_USAGE, "track %s: 'intervals' must be a list" % tr.id)
        cum_offset = Fraction(0)
        file_cursor = 0
        for j, iv in enumerate(raw_ivs):
            I = Interval()
            I.id = iv.get("id", "%s#%d" % (tr.id, j))
            I.decl_index = j
            I.start_q = parse_frac(iv.get("start", 0), "interval start", E_USAGE)
            I.offset = parse_frac(iv.get("offset", 0), "interval offset", E_USAGE)
            I.offset_mode = iv.get("offset_mode", "abs")
            if I.offset_mode not in ("abs", "delta"):
                raise EngineError(E_USAGE, "interval %s: bad offset_mode" % I.id)
            # 偏移默认相对全局时间轴;增量偏移必须由清单显式声明("offset_mode":"delta")
            if I.offset_mode == "delta":
                cum_offset += I.offset
            else:
                cum_offset = I.offset
            try:
                I.length = int(iv.get("length", 0))
            except (TypeError, ValueError):
                raise EngineError(E_USAGE, "interval %s: bad length" % I.id)
            if I.length < 0:
                raise EngineError(E_USAGE, "interval %s: negative length" % I.id)
            I.constant = iv.get("constant")
            if I.constant is not None:
                if isinstance(I.constant, bool) or not isinstance(I.constant, int):
                    raise EngineError(E_SAMPLE_RANGE,
                                      "interval %s: constant sample value must be an integer" % I.id)
                if I.constant < S24_MIN or I.constant > S24_MAX:
                    raise EngineError(
                        E_SAMPLE_RANGE,
                        "interval %s: constant sample value %d out of 24-bit range [%d,%d]"
                        % (I.id, I.constant, S24_MIN, S24_MAX))
                I.file_offset = 0
            else:
                if tr.path is None:
                    raise EngineError(E_MISSING_INPUT,
                                      "interval %s needs a track file" % I.id)
                try:
                    I.file_offset = int(iv.get("file_offset", file_cursor))
                except (TypeError, ValueError):
                    raise EngineError(E_USAGE, "interval %s: bad file_offset" % I.id)
                if I.file_offset < 0:
                    raise EngineError(E_USAGE, "interval %s: negative file_offset" % I.id)
            file_cursor = I.file_offset + I.length
            # 绝对位置 = 分段 BPM 累计(Fraction) + 分数采样偏移,最后仅舍入一次
            I.pos_exact = timeline.samples_at(I.start_q, sr) + cum_offset
            I.start_sample = rhalf_frac(I.pos_exact)
            tr.intervals.append(I)
        # 覆盖语义:后开始者覆盖先开始者;起始相同则声明顺序后者覆盖前者
        tr.intervals.sort(key=lambda x: (x.start_sample, x.decl_index))
        tracks.append(tr)
    return tracks


def check_events(events, tracks, timeline, sr):
    """输入区间与时间轴事件一致性:事件引用的轨道/区间必须存在,
    且区间时长须覆盖事件位置至下一事件位置。在任何临时文件创建之前执行。"""
    evs = []
    for e in events:
        if not isinstance(e, dict) or "track" not in e:
            continue
        q = parse_frac(e.get("position"), "event position")
        evs.append((q, e))
    evs.sort(key=lambda x: x[0])
    positions = [floor_frac(timeline.samples_at(q, sr)) for q, _ in evs]
    tmap = {t.id: t for t in tracks}
    for i, (q, e) in enumerate(evs):
        name = e.get("name", "?")
        tr = tmap.get(e.get("track"))
        if tr is None:
            raise EngineError(E_EVENT_MISMATCH,
                              "event %s references unknown track %s" % (name, e.get("track")))
        iv = None
        for I in tr.intervals:
            if I.id == e.get("interval"):
                iv = I
                break
        if iv is None:
            raise EngineError(E_EVENT_MISMATCH,
                              "event %s references unknown interval %s" % (name, e.get("interval")))
        p = positions[i]
        lo, hi = iv.start_sample, iv.start_sample + iv.length
        if not (lo <= p < hi):
            raise EngineError(
                E_EVENT_MISMATCH,
                "event %s at sample %d outside interval %s [%d,%d)" % (name, p, iv.id, lo, hi))
        if i + 1 < len(evs) and positions[i + 1] > hi:
            raise EngineError(
                E_EVENT_MISMATCH,
                "interval %s [%d,%d) does not cover event %s through next event at sample %d"
                % (iv.id, lo, hi, name, positions[i + 1]))


# ---------------------------------------------------------------- 窗口记账

class WindowLedger(object):
    """记录任一时刻内存中持有的输入样本数(确定性统计,不依赖平台内存测量)。"""

    def __init__(self, limit):
        self.limit = limit
        self.held = 0
        self.peak = 0

    def acquire(self, n):
        self.held += n
        if self.held > self.peak:
            self.peak = self.held

    def release(self, n):
        self.held -= n


# ---------------------------------------------------------------- 恢复日志

def write_log(logpath, obj):
    tmp = logpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    # Windows 上目标文件可能被索引/杀软短暂占用,原子替换需重试
    for _ in range(200):
        try:
            os.replace(tmp, logpath)
            return
        except PermissionError:
            time.sleep(0.01)
    os.replace(tmp, logpath)


def read_and_validate_log(logpath, digest, temp1, temp2, total):
    try:
        with open(logpath, "r", encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        raise LogCorrupt("recovery log is missing or unparseable")
    if not isinstance(log, dict):
        raise LogCorrupt("recovery log is not a JSON object")
    if log.get("version") != 1:
        raise LogCorrupt("bad log version")
    if log.get("digest") != digest:
        raise LogCorrupt("manifest/parameter digest mismatch")
    if log.get("total_frames") != total:
        raise LogCorrupt("total frame count mismatch")
    phase = log.get("phase")
    if phase not in (1, 2):
        raise LogCorrupt("bad phase")
    for k in ("output_samples", "output_bytes", "peak"):
        v = log.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise LogCorrupt("invalid %s (%r)" % (k, log.get(k)))
    if log["output_bytes"] != log["output_samples"] * BYTES_PER_FRAME:
        raise LogCorrupt("byte/sample offset mismatch")
    if log["output_samples"] > total:
        raise LogCorrupt("offset beyond total length")
    temp = temp1 if phase == 1 else temp2
    if not os.path.isfile(temp):
        raise LogCorrupt("temp file for phase %d is missing" % phase)
    if os.path.getsize(temp) < log["output_bytes"]:
        raise LogCorrupt("temp file shorter than logged offset")
    if phase == 2:
        if not os.path.isfile(temp1) or os.path.getsize(temp1) < total * BYTES_PER_FRAME:
            raise LogCorrupt("phase-1 temp file missing or incomplete")
    return log


# ---------------------------------------------------------------- 混音核心

def mix_block(tracks, out_pos, n, ledger, window, reads_out):
    """混合输出区间 [out_pos, out_pos+n) 一帧块,返回 (s24le bytes, 块内峰值)。
    每轨按 (起始采样点, 声明顺序) 依次填充,后者覆盖前者;间隙保持 None -> 静音。
    输入按子块读取,任一时刻持有的输入样本数 <= window(若设置)。"""
    mixL = [0] * n
    mixR = [0] * n
    for tr in tracks:
        ch = tr.channels
        gl = tr.gain * (Fraction(1) - tr.pan) / 2   # 左系数 (1-pan)/2 * gain
        gr = tr.gain * (Fraction(1) + tr.pan) / 2   # 右系数 (1+pan)/2 * gain
        gnl, gdl = gl.numerator, gl.denominator
        gnr, gdr = gr.numerator, gr.denominator
        sub = n if window is None else max(1, window // (2 * ch))
        pos = 0
        while pos < n:
            cnt = min(sub, n - pos)
            gpos = out_pos + pos
            ledger.acquire(cnt * ch)                # 装配缓冲:cnt 帧 * ch 声道
            frames = [None] * cnt
            for I in tr.intervals:
                lo, hi = I.start_sample, I.start_sample + I.length
                a, b = (gpos if gpos > lo else lo), (gpos + cnt if gpos + cnt < hi else hi)
                if a >= b:
                    continue
                k = b - a
                if I.constant is not None:
                    v = I.constant
                    tgt = (v, v) if ch == 2 else v
                    for i in range(a - gpos, b - gpos):
                        frames[i] = tgt
                    reads_out.append({"track": tr.id, "interval": I.id,
                                      "constant": True, "out_frame": a, "frames": k})
                else:
                    foff = I.file_offset + (a - lo)
                    tr.fh.seek(foff * 3 * ch)
                    raw = tr.fh.read(k * 3 * ch)
                    if len(raw) < k * 3 * ch:
                        raise EngineError(E_MISSING_INPUT,
                                          "track file for %s shorter than declared" % tr.id)
                    vals = decode_s24le(raw)
                    ledger.acquire(len(vals))       # 解码缓冲持有的输入样本
                    if ch == 1:
                        for i in range(k):
                            frames[a - gpos + i] = vals[i]
                    else:
                        for i in range(k):
                            frames[a - gpos + i] = (vals[2 * i], vals[2 * i + 1])
                    ledger.release(len(vals))
                    reads_out.append({"track": tr.id, "interval": I.id,
                                      "file_frame": foff, "frames": k})
                    if foff + k > tr.next_frame:
                        tr.next_frame = foff + k
            # 定点应用增益/声像并累加(整数累加,半值远离零)
            if ch == 1:
                for i in range(cnt):
                    v = frames[i]
                    if v is not None:
                        mixL[pos + i] += rhalf_int(v * gnl, gdl)
                        mixR[pos + i] += rhalf_int(v * gnr, gdr)
            else:
                for i in range(cnt):
                    v = frames[i]
                    if v is not None:
                        mixL[pos + i] += rhalf_int(v[0] * gnl, gdl)
                        mixR[pos + i] += rhalf_int(v[1] * gnr, gdr)
            ledger.release(cnt * ch)                # 已处理子块的输入样本立即释放
            pos += cnt
    out = [0] * (2 * n)
    peak = 0
    for i in range(n):
        l = clamp24(mixL[i])
        r = clamp24(mixR[i])
        out[2 * i] = l
        out[2 * i + 1] = r
        al = -l if l < 0 else l
        ar = -r if r < 0 else r
        if al > peak:
            peak = al
        if ar > peak:
            peak = ar
    return encode_s24le(out), peak


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- mix 命令

def cmd_mix(args):
    output = args.output
    temp1 = output + ".tmp1"        # 阶段1:未归一化混音
    temp2 = output + ".tmp2"        # 阶段2:归一化后的最终母带(提交前)
    logpath = output + ".recover.json"
    try:
        return _cmd_mix_inner(args, temp1, temp2, logpath)
    except CommitFailed as e:
        # 受控 I/O 失败:清理本轮临时文件与恢复日志,目标保持不变
        for p in (temp1, temp2, logpath):
            silent_remove(p)
        emit_error(E_COMMIT_FAILED, str(e))
        return 1


def _cmd_mix_inner(args, temp1, temp2, logpath):
    output = args.output
    manifest_bytes, mobj = _load_json_file(args.manifest, "manifest")
    timeline_bytes, tobj = _load_json_file(args.timeline, "timeline")
    timeline, events = Timeline.from_json(tobj)
    sr = args.sample_rate if args.sample_rate else mobj.get("sample_rate")
    if sr not in VALID_RATES:
        raise EngineError(E_USAGE, "sample rate must be one of 44100/48000/96000")
    mdir = os.path.dirname(os.path.abspath(args.manifest))
    # ---- 全部校验(含事件一致性)在创建任何临时文件之前完成 ----
    tracks = parse_tracks(mobj, mdir, timeline, sr)
    check_events(events, tracks, timeline, sr)
    total = 0
    for tr in tracks:
        for I in tr.intervals:
            if I.start_sample + I.length > total:
                total = I.start_sample + I.length
    block = args.block_samples
    if block < 1:
        raise EngineError(E_USAGE, "--block-samples must be >= 1")
    window = args.window_samples
    if window is not None and window < 1:
        raise EngineError(E_USAGE, "--window-samples must be >= 1")

    digest = hashlib.sha256(json.dumps({
        "manifest": hashlib.sha256(manifest_bytes).hexdigest(),
        "timeline": hashlib.sha256(timeline_bytes).hexdigest(),
        "output": os.path.abspath(output),
        "block": block, "window": window, "sample_rate": sr,
    }, sort_keys=True).encode("utf-8")).hexdigest()

    ledger = WindowLedger(window)
    warnings = []
    log_existed = os.path.exists(logpath)
    phase, start1, start2, peak = 1, 0, 0, 0
    if log_existed:
        try:
            log = read_and_validate_log(logpath, digest, temp1, temp2, total)
            phase = log["phase"]
            peak = log["peak"]
            if phase == 1:
                start1 = log["output_samples"]
                with open(temp1, "r+b") as f:
                    f.truncate(log["output_bytes"])   # 截去检查点后的未提交尾部
                silent_remove(temp2)
            else:
                start1 = total
                start2 = log["output_samples"]
                with open(temp2, "r+b") as f:
                    f.truncate(log["output_bytes"])
        except LogCorrupt as e:
            # 可恢复告警:记入最终报告,从头重算
            warnings.append({"code": E_LOG_CORRUPT,
                             "message": "discarding invalid recovery log: %s" % e})
            silent_remove(temp1)
            silent_remove(temp2)
            silent_remove(logpath)
            phase, start1, start2, peak = 1, 0, 0, 0
    crash_armed = not log_existed   # 崩溃注入仅在全新运行时武装,保证重启同一命令可完成

    for tr in tracks:
        if tr.path is not None:
            try:
                tr.fh = open(tr.path, "rb")
            except OSError:
                raise EngineError(E_MISSING_INPUT, "cannot open track file: %s" % tr.path)
    try:
        # ================= 阶段 1:流式混音 + 求全局峰值 =================
        if phase == 1:
            f1 = open(temp1, "r+b" if start1 else "w+b")
            f1.seek(start1 * BYTES_PER_FRAME)
            done = start1
            bidx = start1 // block
            try:
                while done < total:
                    n = min(block, total - done)
                    reads = []
                    data, bpeak = mix_block(tracks, done, n, ledger, window, reads)
                    if bpeak > peak:
                        peak = bpeak
                    f1.write(data)
                    done += n
                    f1.flush()
                    os.fsync(f1.fileno())             # 先刷新临时文件,再原子更新日志
                    write_log(logpath, {
                        "version": 1, "digest": digest, "phase": 1,
                        "total_frames": total,
                        "output_samples": done, "output_bytes": done * BYTES_PER_FRAME,
                        "peak": peak,
                        "input_positions": {t.id: t.next_frame for t in tracks},
                    })
                    if args.trace_window:
                        print(json.dumps({"type": "trace_window", "phase": 1, "block": bidx,
                                          "out": [done - n, done], "reads": reads,
                                          "held_peak": ledger.peak}))
                    bidx += 1
                    if (crash_armed and args.crash_after_output_samples is not None
                            and done >= args.crash_after_output_samples):
                        os._exit(3)                   # 模拟强制终止:不执行任何清理
            except OSError as e:
                f1.close()
                raise CommitFailed("phase-1 temp write failed: %s" % e)
            f1.close()
            # 阶段转换检查点(区分归一化两阶段)
            write_log(logpath, {
                "version": 1, "digest": digest, "phase": 2, "total_frames": total,
                "output_samples": 0, "output_bytes": 0, "peak": peak,
                "input_positions": {t.id: t.next_frame for t in tracks},
            })

        # ================= 阶段 2:Fraction 归一化增益 + 二次写入 =================
        # 峰值已达/超过满幅(注意 -8388608 的绝对值为 8388608)时增益为 1:
        # 饱和信号保持原样,只有需要提升的小信号才按 S24_MAX/peak 缩放。
        gain = Fraction(S24_MAX, peak) if 0 < peak < S24_MAX else Fraction(1)
        gn, gd = gain.numerator, gain.denominator
        side_hashes = []
        master_hasher = hashlib.sha256()
        if start2:
            with open(temp2, "rb") as fr:             # 续算:重放已提交前缀的哈希
                for _ in range(start2 // block):
                    chunk = fr.read(block * BYTES_PER_FRAME)
                    master_hasher.update(chunk)
                    side_hashes.append(hashlib.sha256(chunk).hexdigest())
        f1 = open(temp1, "rb")
        f1.seek(start2 * BYTES_PER_FRAME)
        f2 = open(temp2, "r+b" if start2 else "w+b")
        f2.seek(start2 * BYTES_PER_FRAME)
        done = start2
        bidx = start2 // block
        try:
            while done < total:
                n = min(block, total - done)
                raw = f1.read(n * BYTES_PER_FRAME)
                if len(raw) < n * BYTES_PER_FRAME:
                    raise CommitFailed("phase-1 temp file truncated")
                vals = decode_s24le(raw)
                out = [clamp24(rhalf_int(v * gn, gd)) for v in vals]
                data = encode_s24le(out)
                f2.write(data)
                master_hasher.update(data)
                side_hashes.append(hashlib.sha256(data).hexdigest())
                done += n
                f2.flush()
                os.fsync(f2.fileno())
                write_log(logpath, {
                    "version": 1, "digest": digest, "phase": 2,
                    "total_frames": total,
                    "output_samples": done, "output_bytes": done * BYTES_PER_FRAME,
                    "peak": peak,
                    "input_positions": {t.id: t.next_frame for t in tracks},
                })
                if args.trace_window:
                    print(json.dumps({"type": "trace_window", "phase": 2, "block": bidx,
                                      "out": [done - n, done], "reads": [],
                                      "held_peak": ledger.peak}))
                bidx += 1
                if (args.fail_after_output_bytes is not None
                        and done * BYTES_PER_FRAME >= args.fail_after_output_bytes):
                    raise CommitFailed(
                        "injected I/O failure after %d output bytes"
                        % args.fail_after_output_bytes)
                if (crash_armed and args.crash_after_output_samples is not None
                        and done >= args.crash_after_output_samples):
                    os._exit(3)
        except OSError as e:
            raise CommitFailed("phase-2 temp write failed: %s" % e)
        finally:
            f1.close()
            f2.close()
    finally:
        for tr in tracks:
            if tr.fh is not None:
                tr.fh.close()

    # ================= 原子提交 =================
    os.replace(temp2, output)         # 同目录原子替换;此前目标保持不变
    silent_remove(temp1)
    silent_remove(logpath)            # 完成后删除恢复日志

    sidecar_path = args.sidecar or (output + ".sidecar.json")
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump({"format": "s24le", "channels": OUT_CHANNELS, "sample_rate": sr,
                   "block_samples": block, "frames": total,
                   "sha256": side_hashes}, f)

    report = {"type": "report", "command": "mix", "status": "ok", "output": output,
              "frames": total, "sample_rate": sr, "peak": peak,
              "normalize_gain": str(gain), "sha256": master_hasher.hexdigest(),
              "sidecar": sidecar_path, "warnings": warnings,
              "block_samples": block, "window_samples": window,
              "peak_held_samples": ledger.peak}
    if args.trace_peak_window:
        print(json.dumps({"type": "trace_peak_window",
                          "peak_held_samples": ledger.peak}))
    print(json.dumps(report))
    return 0


# ---------------------------------------------------------------- copy 命令(流式读写)

def cmd_copy(args):
    if not os.path.isfile(args.input):
        raise EngineError(E_MISSING_INPUT, "input not found: %s" % args.input)
    ch = args.channels
    if ch not in (1, 2):
        raise EngineError(E_USAGE, "--channels must be 1 or 2")
    block = args.block_samples
    window = args.window_samples
    if block < 1 or (window is not None and window < 1):
        raise EngineError(E_USAGE, "block/window must be >= 1")
    chunk = block if window is None else max(1, min(block, window // ch))
    size = os.path.getsize(args.input)
    if size % (3 * ch):
        raise EngineError(E_USAGE, "input size is not a multiple of the frame size")
    total = size // (3 * ch)
    ledger = WindowLedger(window)
    temp = args.output + ".tmp"
    silent_remove(temp)
    try:
        fin = open(args.input, "rb")
        fo = open(temp, "wb")
    except OSError as e:
        raise EngineError(E_MISSING_INPUT, str(e))
    done = 0
    bidx = 0
    try:
        while done < total:
            n = min(chunk, total - done)
            raw = fin.read(n * 3 * ch)
            if len(raw) < n * 3 * ch:
                raise EngineError(E_MISSING_INPUT, "input file truncated")
            vals = decode_s24le(raw)
            ledger.acquire(len(vals))
            held = ledger.held
            data = encode_s24le(vals)             # 解码->重编码,验证三字节模式往返一致
            ledger.release(len(vals))
            fo.write(data)
            done += n
            if args.trace_window:
                print(json.dumps({"type": "trace_window", "block": bidx,
                                  "frames": [done - n, done], "held": held}))
            bidx += 1
        fo.flush()
        os.fsync(fo.fileno())
    except OSError as e:
        fin.close()
        fo.close()
        silent_remove(temp)
        emit_error(E_COMMIT_FAILED, "copy failed: %s" % e)
        return 1
    fin.close()
    fo.close()
    os.replace(temp, args.output)
    if args.trace_peak_window:
        print(json.dumps({"type": "trace_peak_window",
                          "peak_held_samples": ledger.peak}))
    print(json.dumps({"type": "report", "command": "copy", "status": "ok",
                      "input": args.input, "output": args.output, "frames": total,
                      "channels": ch, "peak_held_samples": ledger.peak,
                      "sha256": sha256_of_file(args.output)}))
    return 0


# ---------------------------------------------------------------- timeline / positions

def cmd_timeline(args):
    _, tobj = _load_json_file(args.timeline, "timeline")
    timeline, events = Timeline.from_json(tobj)
    sr = args.sample_rate
    if sr not in VALID_RATES:
        raise EngineError(E_USAGE, "sample rate must be one of 44100/48000/96000")
    out = {"sample_rate": sr, "events": []}
    for e in events:
        q = parse_frac(e.get("position"), "event position")
        s = floor_frac(timeline.samples_at(q, sr))   # Fraction 精确计算后向下取整
        out["events"].append({"name": e.get("name"), "position": str(q), "sample": s})
    print(json.dumps(out, indent=2))
    return 0


def cmd_positions(args):
    _, mobj = _load_json_file(args.manifest, "manifest")
    _, tobj = _load_json_file(args.timeline, "timeline")
    timeline, _ = Timeline.from_json(tobj)
    sr = args.sample_rate if args.sample_rate else mobj.get("sample_rate")
    if sr not in VALID_RATES:
        raise EngineError(E_USAGE, "sample rate must be one of 44100/48000/96000")
    mdir = os.path.dirname(os.path.abspath(args.manifest))
    tracks = parse_tracks(mobj, mdir, timeline, sr)
    rows = []
    for tr in tracks:
        for I in sorted(tr.intervals, key=lambda x: x.decl_index):
            rows.append({"track": tr.id, "interval": I.id,
                         "position_exact": str(I.pos_exact),
                         "sample": I.start_sample, "length": I.length})
    print(json.dumps({"sample_rate": sr, "intervals": rows}, indent=2))
    return 0


# ---------------------------------------------------------------- validate 命令

def cmd_validate(args):
    if not os.path.isfile(args.master):
        raise EngineError(E_MISSING_INPUT, "master not found: %s" % args.master)
    sidecar_bytes, side = _load_json_file(args.sidecar, "sidecar")
    try:
        B = int(side["block_samples"])
        hashes = list(side["sha256"])
    except (KeyError, TypeError, ValueError):
        raise EngineError(E_USAGE, "sidecar missing block_samples/sha256")
    logpath = args.master + ".validate.recover.json"
    digest = hashlib.sha256(
        os.path.abspath(args.master).encode("utf-8") + b"|" + sidecar_bytes
        + str(os.path.getsize(args.master)).encode("utf-8")).hexdigest()
    start = 0
    mism = []
    warnings = []
    log_existed = os.path.exists(logpath)
    if log_existed:
        try:
            with open(logpath, "r", encoding="utf-8") as f:
                log = json.load(f)
            if (not isinstance(log, dict) or log.get("digest") != digest
                    or not isinstance(log.get("next_block"), int)
                    or log["next_block"] < 0 or log["next_block"] > len(hashes)
                    or not isinstance(log.get("mismatches"), list)):
                raise LogCorrupt("invalid validate recovery log")
            start = log["next_block"]
            mism = list(log["mismatches"])
        except (OSError, ValueError, LogCorrupt) as e:
            warnings.append({"code": E_LOG_CORRUPT,
                             "message": "discarding invalid validate log: %s" % e})
            silent_remove(logpath)
            start = 0
            mism = []
    crash_armed = not log_existed
    with open(args.master, "rb") as f:
        f.seek(start * B * BYTES_PER_FRAME)
        for i in range(start, len(hashes)):
            chunk = f.read(B * BYTES_PER_FRAME)
            if hashlib.sha256(chunk).hexdigest() != hashes[i]:
                mism.append(i)
            write_log(logpath, {"version": 1, "digest": digest,
                                "next_block": i + 1, "mismatches": mism})
            if (crash_armed and args.crash_after_blocks is not None
                    and (i + 1) >= args.crash_after_blocks):
                os._exit(3)
    silent_remove(logpath)
    ok = not mism
    print(json.dumps({"type": "report", "command": "validate", "ok": ok,
                      "mismatches": mism, "blocks": len(hashes),
                      "warnings": warnings}))
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

class Parser(argparse.ArgumentParser):
    def error(self, message):
        emit_error(E_USAGE, message)
        sys.exit(2)


def build_parser():
    p = Parser(prog="engine.py",
               description="streaming 24-bit little-endian PCM mixing engine")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("timeline", help="locate timeline events on the sample grid")
    t.add_argument("--timeline", required=True)
    t.add_argument("--sample-rate", type=int, required=True)
    t.set_defaults(func=cmd_timeline)

    po = sub.add_parser("positions", help="exact Fraction positions of manifest intervals")
    po.add_argument("--manifest", required=True)
    po.add_argument("--timeline", required=True)
    po.add_argument("--sample-rate", type=int)
    po.set_defaults(func=cmd_positions)

    m = sub.add_parser("mix", help="stream-mix tracks into a normalized stereo master")
    m.add_argument("--manifest", required=True)
    m.add_argument("--timeline", required=True)
    m.add_argument("--output", required=True)
    m.add_argument("--sample-rate", type=int)
    m.add_argument("--block-samples", type=int, default=4096)
    m.add_argument("--window-samples", type=int)
    m.add_argument("--trace-window", action="store_true")
    m.add_argument("--trace-peak-window", action="store_true")
    m.add_argument("--fail-after-output-bytes", type=int)
    m.add_argument("--crash-after-output-samples", type=int)
    m.add_argument("--sidecar")
    m.set_defaults(func=cmd_mix)

    c = sub.add_parser("copy", help="streaming 24-bit PCM read/write with window limit")
    c.add_argument("--input", required=True)
    c.add_argument("--output", required=True)
    c.add_argument("--channels", type=int, required=True)
    c.add_argument("--block-samples", type=int, default=65536)
    c.add_argument("--window-samples", type=int)
    c.add_argument("--trace-window", action="store_true")
    c.add_argument("--trace-peak-window", action="store_true")
    c.set_defaults(func=cmd_copy)

    v = sub.add_parser("validate", help="re-hash master blocks against sidecar")
    v.add_argument("--master", required=True)
    v.add_argument("--sidecar", required=True)
    v.add_argument("--crash-after-blocks", type=int)
    v.set_defaults(func=cmd_validate)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EngineError as e:
        emit_error(e.code, e.message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
