from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from spkvc.dataset import Dataset, generate_dataset
from spkvc.pipeline import ConversionEngine


class DatasetTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="spkvc-tests-"))
        cls.dataset_root = cls.tmp / "data"
        cls.dataset = generate_dataset(cls.dataset_root, force=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def engine(self, name: str, **kwargs) -> ConversionEngine:
        return ConversionEngine(
            dataset=Dataset(self.dataset_root),
            state_dir=self.tmp / name,
            output_dir=self.tmp / name / "outputs",
            **kwargs,
        )

