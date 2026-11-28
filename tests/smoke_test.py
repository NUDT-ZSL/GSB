#!/usr/bin/env python3
"""End-to-end smoke test for the SDF mesh workbench.

Validates the acceptance points against the deterministic project:
isosurface zero-ness, manifold defect lists, base/wall verdicts,
simplification volume error, error codes and version records.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workbench import mesher, meshcheck, pipeline  # noqa: E402
from workbench.db import Store  # noqa: E402
from workbench.errors import WorkbenchError  # noqa: E402
from workbench.generator import generate_project, get_asset  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"[{status}] {label} {detail}")
    if not cond:
        FAILURES.append(label)


def main():
    project = generate_project()
    names = ["castle", "tree", "crystal_cluster", "mountain", "river",
             "cave"]

    # determinism: two generations are identical
    check("deterministic project",
          json.dumps(generate_project(), sort_keys=True)
          == json.dumps(project, sort_keys=True))

    tmp = tempfile.mkdtemp(prefix="workbench-test-")
    store = Store(os.path.join(tmp, "test.db"))
    try:
        for name in names:
            asset = get_asset(project, name)
            report, meshes = pipeline.run_asset(project, asset)
            ex = report["extraction"]
            mc = report["mesh_check"]
            check(f"{name}: isosurface zero-ness",
                  ex["max_abs_sdf_residual"] < 1e-6,
                  f"residual={ex['max_abs_sdf_residual']:.2e}")
            check(f"{name}: closed & manifold",
                  mc["is_closed"] and mc["is_manifold"]
                  and not mc["defects"], str(mc["defects"]))
            sv = report["simplification"]["validation"]
            check(f"{name}: simplification volume error",
                  sv["volume_error"]
                  <= asset["constraints"]["volume_tolerance"],
                  f"vol_err={sv['volume_error']:.4f}")
            check(f"{name}: simplified mesh closed", sv["closed"])
            version = store.save_version(
                name, asset["constraints"], report, meshes)
            check(f"{name}: version stored", version == 1)

        # expected diagnostic verdicts on the deterministic project
        verdicts = {r: pipeline.run_asset(
            project, get_asset(project, r))[0] for r in names}
        check("castle printable", verdicts["castle"]["printable"])
        check("tree base not flat (rounded trunk)",
              not verdicts["tree"]["base_flatness"]["flat"])
        check("cave has thin-wall regions",
              len(verdicts["cave"]["wall_thickness"]["regions"]) > 0)
        check("mountain has thin-wall regions",
              len(verdicts["mountain"]["wall_thickness"]["regions"]) > 0)

        # error codes
        for sample, code in (("sample_empty_intersection",
                              "E_CSG_EMPTY_INTERSECTION"),
                             ("sample_empty_subtrahend",
                              "E_CSG_EMPTY_SUBTRAHEND")):
            try:
                pipeline.run_asset(project, get_asset(project, sample))
                check(f"{sample} raises {code}", False)
            except WorkbenchError as err:
                check(f"{sample} raises {code}", err.code == code,
                      err.code)

        # invalid parameters
        bad = get_asset(project, "castle")
        import copy
        for key, code in (("resolution", "E_INVALID_RESOLUTION"),
                          ("iso_threshold", "E_INVALID_THRESHOLD"),
                          ("target_faces", "E_INVALID_TARGET_FACES")):
            mutant = copy.deepcopy(bad)
            mutant["constraints"][key] = 0
            try:
                pipeline.run_asset(project, mutant)
                check(f"{key}=0 raises {code}", False)
            except WorkbenchError as err:
                check(f"{key}=0 raises {code}", err.code == code, err.code)

        # failed runs must not create versions
        check("no versions for boundary samples",
              store.list_versions("sample_empty_intersection") == [])

        # version diff
        diff = store.diff_versions("castle", 1, 1)
        check("diff versions", diff["vertex_count"]["delta"] == 0)
        check("version records complete",
              len(store.list_versions()) == len(names))
    finally:
        store.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
