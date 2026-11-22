"""v1/v2/v3 三种合成配置读取器。

v1: 扁平位置行式   ``V1|<speaker>|<style>|<prosody_scale>|<precision>|<vocoder_version>``
v2: 带字段名行式   ``V2|speaker=..|style=..|prosody_scale=..|precision=..|vocoder=..[|duration_scale=..]``
v3: 结构化嵌套 JSON，含 version 字段。
"""

import json

from .errors import E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED, TtsError

KNOWN_VERSIONS = ("v1", "v2", "v3")

V1_FIELDS = ("speaker", "style", "prosody_scale", "precision", "vocoder")
V2_REQUIRED = ("speaker", "style", "prosody_scale", "precision", "vocoder")
V2_OPTIONAL = ("duration_scale",)
V3_REQUIRED = ("speaker", "style", "prosody", "precision", "vocoder")

DEFAULTS = {
    "style": "neutral",
    "prosody_scale": 1.0,
    "precision": "fp32",
    "duration_scale": 1.0,
}


def _corrupt(message, line_no, field):
    return TtsError(E_FORMAT_CORRUPT, message, line=line_no, field=field)


def _parse_float(raw, line_no, field):
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise _corrupt(f"数值不可解析: {raw!r}", line_no, field)


def _canonical(source_version, speaker, style, prosody_scale, precision,
               vocoder, duration_scale=None, style_inherit=None,
               duration_curve=None, vocoder_params=None):
    """统一为内部规范化配置字典，保留来源版本。"""
    return {
        "source_version": source_version,
        "speaker": speaker,
        "style": {"tag": style, "inherit": style_inherit},
        "prosody": {
            "scale": prosody_scale,
            "duration_scale": duration_scale,
            "duration_curve": duration_curve,
        },
        "precision": precision,
        "vocoder": {
            "name": "griffin",
            "version": vocoder,
            "params": vocoder_params or {"n_fft": 512, "hop": 128},
        },
    }


def _parse_v1(lines):
    line_no = 1
    head = lines[0]
    if not head.startswith("V1|"):
        raise _corrupt("头部截断或缺失 V1 版本头", line_no, "header")
    parts = head.split("|")
    fields = parts[1:]
    if len(fields) != len(V1_FIELDS):
        raise _corrupt(
            f"竖线字段数不符: 期望 {len(V1_FIELDS)} 实得 {len(fields)}",
            line_no, "field_count")
    speaker, style, prosody_raw, precision, vocoder = fields
    if not speaker:
        raise _corrupt("必填字段缺失或为空", line_no, "speaker")
    prosody_scale = _parse_float(prosody_raw, line_no, "prosody_scale")
    return _canonical("v1", speaker, style or None, prosody_scale,
                      precision or None, vocoder)


def _parse_v2(lines):
    line_no = 1
    head = lines[0]
    if not head.startswith("V2|"):
        raise _corrupt("头部截断或缺失 V2 版本头", line_no, "header")
    kv = {}
    for item in head.split("|")[1:]:
        if "=" not in item:
            raise _corrupt(f"键值对缺少 '=': {item!r}", line_no, item)
        key, _, value = item.partition("=")
        kv[key.strip()] = value.strip()
    for field in V2_REQUIRED:
        if field not in kv or kv[field] == "":
            raise _corrupt("必填字段缺失", line_no, field)
    prosody_scale = _parse_float(kv["prosody_scale"], line_no, "prosody_scale")
    duration_scale = None
    if kv.get("duration_scale"):
        duration_scale = _parse_float(kv["duration_scale"], line_no, "duration_scale")
    return _canonical("v2", kv["speaker"], kv["style"], prosody_scale,
                      kv["precision"], kv["vocoder"],
                      duration_scale=duration_scale)


def _parse_v3(text):
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _corrupt(f"JSON 解析失败: {exc.msg}", exc.lineno, "json")
    if not isinstance(obj, dict):
        raise _corrupt("v3 配置须为 JSON 对象", 1, "root")
    for field in V3_REQUIRED:
        if field not in obj:
            raise _corrupt("必填字段缺失", 1, field)
    style = obj["style"]
    prosody = obj["prosody"]
    vocoder = obj["vocoder"]
    if not isinstance(style, dict) or "tag" not in style:
        raise _corrupt("style 须为含 tag 的对象", 1, "style.tag")
    if not isinstance(prosody, dict) or "scale" not in prosody:
        raise _corrupt("prosody 须为含 scale 的对象", 1, "prosody.scale")
    if not isinstance(vocoder, dict) or "version" not in vocoder:
        raise _corrupt("vocoder 须为含 version 的对象", 1, "vocoder.version")
    prosody_scale = _parse_float(prosody["scale"], 1, "prosody.scale")
    duration_scale = prosody.get("duration_scale")
    if duration_scale is not None:
        duration_scale = _parse_float(duration_scale, 1, "prosody.duration_scale")
    curve = prosody.get("duration_curve")
    if curve is not None:
        if not isinstance(curve, list) or not curve:
            raise _corrupt("duration_curve 须为非空数组", 1, "prosody.duration_curve")
        curve = [_parse_float(v, 1, "prosody.duration_curve") for v in curve]
    return _canonical(
        "v3", obj["speaker"], style["tag"], prosody_scale,
        obj["precision"], vocoder["version"],
        duration_scale=duration_scale,
        style_inherit=style.get("inherit"),
        duration_curve=curve,
        vocoder_params=vocoder.get("params"),
    )


def detect_version(text):
    """按版本头或字段集分派。"""
    first = text.strip().splitlines()[0] if text.strip() else ""
    if first.startswith("V1|") or first == "V1":
        return "v1"
    if first.startswith("V2|") or first == "V2":
        return "v2"
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return "v3"  # 交给 v3 解析器报 E_FORMAT_CORRUPT
        version = obj.get("version")
        if version in KNOWN_VERSIONS:
            return version
        raise TtsError(E_VERSION_UNSUPPORTED,
                       f"未知版本: {version!r}", version=version)
    raise TtsError(E_VERSION_UNSUPPORTED,
                   f"无法识别的配置版本头: {first[:32]!r}", header=first[:32])


def load_config(text):
    """读取任意版本配置，返回带 source_version 的规范化字典。"""
    version = detect_version(text)
    if version == "v1":
        return _parse_v1(text.strip().splitlines())
    if version == "v2":
        return _parse_v2(text.strip().splitlines())
    if version == "v3":
        return _parse_v3(text)
    raise TtsError(E_VERSION_UNSUPPORTED, f"未知版本: {version}", version=version)


def load_config_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return load_config(fh.read())
