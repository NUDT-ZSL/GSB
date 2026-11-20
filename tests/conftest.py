from __future__ import annotations

from pathlib import Path

import pytest

from dialog_emotion.samples import SampleBundle, generate_bundle


@pytest.fixture(scope="session")
def small_bundle() -> SampleBundle:
    return generate_bundle(
        duration_ms_by_dialog={"dlg-001": 1800, "dlg-002": 1500},
        dialog_seeds={"dlg-001": 11, "dlg-002": 22},
        speaker_ids=["spk_a", "spk_b", "spk_c"],
        frame_variants=((20.0, 10.0), (20.0, 15.0)),
    )


@pytest.fixture()
def bundle(small_bundle: SampleBundle) -> SampleBundle:
    return small_bundle.copy()


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path / "work"
