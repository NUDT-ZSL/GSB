"""End-to-end acceptance checks for the deterministic NER component."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "work")
SAMPLES = os.path.join(WORK, "samples")

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" - " + detail) if detail else ""))


def run(mode, report, snapshot, edits="edits.json", concurrency=None,
        batch_sleep_ms=None):
    cmd = [sys.executable, os.path.join(ROOT, "main.py"), mode,
           "--sample-dir", SAMPLES,
           "--report", report,
           "--snapshot-dir", snapshot,
           "--edits", edits]
    if concurrency:
        cmd += ["--concurrency", str(concurrency)]
    if batch_sleep_ms is not None:
        cmd += ["--batch-sleep-ms", str(batch_sleep_ms)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
    return proc.returncode


def load_report(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def comparable(report):
    return {
        "sentences": report["sentences"],
        "corrections": report["corrections"],
        "failures": report["failures"],
    }


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    sys.path.insert(0, ROOT)
    from generate_samples import generate
    generate(SAMPLES)

    rep_full = os.path.join(WORK, "report_full.json")
    rep_incr = os.path.join(WORK, "report_incr.json")
    snap_a = os.path.join(WORK, "snap_a")
    snap_b = os.path.join(WORK, "snap_b")

    # 1. full run from scratch vs batched incremental run
    check("full run exits 0", run("full", rep_full, snap_a) == 0)
    check("incremental run exits 0", run("incr", rep_incr, snap_b) == 0)
    full = load_report(rep_full)
    incr = load_report(rep_incr)
    check("incremental == full (sentences/corrections/failures)",
          comparable(full) == comparable(incr))
    n_entities = sum(len(s["entities"]) for s in full["sentences"])
    check("entities extracted", n_entities > 0, "total=%d" % n_entities)
    check("corrections recorded", len(full["corrections"]) > 0,
          "total=%d" % len(full["corrections"]))
    check("incremental recomputed sentences",
          incr["stats"]["recomputed"] > 0,
          "reused=%d recomputed=%d" % (incr["stats"]["reused"],
                                       incr["stats"]["recomputed"]))

    # 2. concurrency determinism: 1 vs 8 workers
    rep_c1 = os.path.join(WORK, "report_c1.json")
    rep_c8 = os.path.join(WORK, "report_c8.json")
    run("full", rep_c1, os.path.join(WORK, "snap_c1"), concurrency=1)
    run("full", rep_c8, os.path.join(WORK, "snap_c8"), concurrency=8)
    check("concurrency 1 == 8",
          comparable(load_report(rep_c1)) == comparable(load_report(rep_c8)))

    # 3. precise invalidation: single-entry delete only recomputes hits
    snap_c = os.path.join(WORK, "snap_c")
    rep_seed = os.path.join(WORK, "report_seed.json")
    rep_del = os.path.join(WORK, "report_del.json")
    run("incr", rep_seed, snap_c, edits="edits_empty.json")
    run("incr", rep_del, snap_c, edits="edits_delete.json")
    seed = load_report(rep_seed)
    dele = load_report(rep_del)
    expected = set()
    with open(os.path.join(SAMPLES, "corpus.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)
            if "张三" in obj["chars"].replace(" ", ""):
                expected.add(obj["sent_id"])
    expected -= {"s9000"}  # failed sentence never recomputed
    got = set(dele["stats"]["recomputed_sents"])
    check("precise invalidation on delete",
          got == expected,
          "expected=%s got=%s" % (sorted(expected), sorted(got)))
    check("resume run reuses unaffected sentences",
          dele["stats"]["reused"] > 0,
          "reused=%d recomputed=%d" % (dele["stats"]["reused"],
                                       dele["stats"]["recomputed"]))
    reused_ok = all(
        next(s for s in dele["sentences"] if s["sent_id"] == sid)
        == next(s for s in seed["sentences"] if s["sent_id"] == sid)
        for sid in dele["stats"]["reused_sents"])
    check("reused sentences byte-identical", reused_ok)

    # 4. exception isolation and business codes
    codes = {(f["id"], f["code"]) for f in full["failures"]}
    check("E_CORPUS_INVALID for bad corpus lines",
          any(fid.startswith("line:") and code == "E_CORPUS_INVALID"
              for fid, code in codes))
    check("E_CORPUS_INVALID for control-char sentence",
          ("s9000", "E_CORPUS_INVALID") in codes)
    check("E_DICT_INVALID for illegal entry type",
          any(code == "E_DICT_INVALID" for _, code in codes))
    check("failed sentence produces no entities",
          all(s["sent_id"] != "s9000" for s in full["sentences"]))
    check("failed sentence stays failed in incremental",
          all(s["sent_id"] != "s9000" for s in incr["sentences"]))

    # 5. snapshot recovery: tampered manifest -> E_CONFIG_MISMATCH + restart
    manifest_path = os.path.join(snap_b, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["dict_hash"] = "0" * 64
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    rep_rec = os.path.join(WORK, "report_recover.json")
    run("incr", rep_rec, snap_b)
    rec = load_report(rep_rec)
    check("E_CONFIG_MISMATCH on tampered manifest",
          any(f["code"] == "E_CONFIG_MISMATCH" for f in rec["failures"]))
    check("recovery restarts and matches full run",
          {k: rec[k] for k in ("sentences", "corrections")}
          == {k: full[k] for k in ("sentences", "corrections")})
    check("no half-replaced snapshot residue",
          not os.path.exists(snap_b + ".tmp")
          and not os.path.exists(snap_b + ".old"))

    # 6. graceful exit: SIGTERM finishes current batch, exit code 0
    snap_d = os.path.join(WORK, "snap_d")
    rep_sig = os.path.join(WORK, "report_sig.json")
    cmd = [sys.executable, os.path.join(ROOT, "main.py"), "incr",
           "--sample-dir", SAMPLES, "--report", rep_sig,
           "--snapshot-dir", snap_d, "--edits", "edits_signal.json",
           "--batch-sleep-ms", "400"]
    proc = subprocess.Popen(cmd)
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=30)
    check("SIGTERM exits with code 0", rc == 0, "rc=%s" % rc)
    check("snapshot persisted on signal",
          os.path.isfile(os.path.join(snap_d, "manifest.json")))
    check("report written on signal", os.path.isfile(rep_sig))

    # 7. determinism: identical inputs -> byte-identical reports
    rep_det = os.path.join(WORK, "report_det.json")
    run("full", rep_det, snap_a)
    with open(rep_full, "rb") as fh:
        raw_a = fh.read()
    with open(rep_det, "rb") as fh:
        raw_b = fh.read()
    check("repeated full run byte-identical", raw_a == raw_b)

    # 8. model shape mismatch -> E_MODEL_SHAPE_MISMATCH
    bad_dir = os.path.join(WORK, "samples_bad_model")
    shutil.copytree(SAMPLES, bad_dir)
    with open(os.path.join(bad_dir, "model.json"), encoding="utf-8") as fh:
        bad_model = json.load(fh)
    bad_model["emission"]["O"] = bad_model["emission"]["O"][:-1]
    with open(os.path.join(bad_dir, "model.json"), "w", encoding="utf-8") as fh:
        json.dump(bad_model, fh, ensure_ascii=False)
    rep_bad = os.path.join(WORK, "report_bad_model.json")
    cmd = [sys.executable, os.path.join(ROOT, "main.py"), "full",
           "--sample-dir", bad_dir, "--report", rep_bad,
           "--snapshot-dir", os.path.join(WORK, "snap_bad")]
    subprocess.run(cmd, capture_output=True, text=True)
    bad_rep = load_report(rep_bad)
    check("E_MODEL_SHAPE_MISMATCH on bad model",
          any(f["code"] == "E_MODEL_SHAPE_MISMATCH"
              for f in bad_rep["failures"]))

    failed = [c for c in CHECKS if not c[1]]
    print("\n%d/%d checks passed" % (len(CHECKS) - len(failed), len(CHECKS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
