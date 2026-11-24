import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from drift_engine import samplegen


@pytest.fixture()
def sample_workspace(tmp_path):
    output_dir = tmp_path / "samples"
    samplegen.generate(str(output_dir))
    return output_dir


@pytest.fixture()
def engine_factory(tmp_path):
    from drift_engine.spec import BaselineSpec
    from drift_engine.store import DriftEngine

    created = []

    def _factory(spec_payload=None):
        payload = spec_payload or samplegen.build_spec()
        spec = BaselineSpec(payload)
        db_path = tmp_path / f"ledger-{len(created)}.db"
        cp_dir = tmp_path / f"cp-{len(created)}"
        engine = DriftEngine(str(db_path), spec, str(cp_dir))
        created.append(engine)
        return engine, spec, tmp_path

    yield _factory
    for engine in created:
        engine.close()
