from __future__ import annotations

import json

from sft_pipeline.engine import PipelineEngine
from sft_pipeline.errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_DUP_SAMPLE,
    E_LANG_INVALID,
    E_RESOURCE_LIMIT,
    E_TEMPLATE_INVALID,
)
from sft_pipeline.validation import load_bundle

from .helpers import WorkspaceTestCase


class ValidationTests(WorkspaceTestCase):
    def test_business_validation_codes_and_fields(self) -> None:
        cases = []

        workspace = self.make_workspace("duplicate")
        lines = workspace.samples_path.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["output"] = first["output"] + " duplicate"
        lines.append(json.dumps(first, ensure_ascii=False))
        workspace.samples_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cases.append((workspace, E_DUP_SAMPLE, "sample_id"))

        workspace = self.make_workspace("lang")
        records = [
            json.loads(line)
            for line in workspace.samples_path.read_text(encoding="utf-8").splitlines()
        ]
        records[0]["lang"] = "xx"
        workspace.samples_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
            + "\n",
            encoding="utf-8",
        )
        cases.append((workspace, E_LANG_INVALID, "lang"))

        workspace = self.make_workspace("template")
        library = self.read_json(workspace.templates_path)
        library["templates"][0]["pattern"] = "{instruction} {input}"
        self.write_json(workspace.templates_path, library)
        cases.append((workspace, E_TEMPLATE_INVALID, "templates[0].pattern"))

        workspace = self.make_workspace("weight")
        rules = self.read_json(workspace.rules_path)
        rules["quality"]["weights"]["length"] = 0
        self.write_json(workspace.rules_path, rules)
        cases.append(
            (workspace, E_CONFIG_INVALID, "quality.weights.length")
        )

        workspace = self.make_workspace("threshold")
        rules = self.read_json(workspace.rules_path)
        rules["dedupe"]["threshold"] = 1.5
        self.write_json(workspace.rules_path, rules)
        cases.append((workspace, E_CONFIG_INVALID, "dedupe.threshold"))

        workspace = self.make_workspace("quota")
        quotas = self.read_json(workspace.quotas_path)
        quotas["targets"][0]["target"] = 0
        self.write_json(workspace.quotas_path, quotas)
        cases.append(
            (workspace, E_CONFIG_INVALID, "quotas.targets[0].target")
        )

        for workspace, code, field in cases:
            with self.subTest(code=code, field=field):
                with self.assertRaises(BusinessError) as raised:
                    load_bundle(workspace)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.field, field)

    def test_resource_limits_and_parallel_degradation(self) -> None:
        workspace = self.make_workspace("sample-limit")
        config = self.read_json(workspace.pipeline_config_path)
        config["limits"]["max_samples_per_stage"] = 10
        self.write_json(workspace.pipeline_config_path, config)
        with self.assertRaises(BusinessError) as raised:
            PipelineEngine(workspace).run()
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(
            raised.exception.field, "limits.max_samples_per_stage"
        )

        workspace = self.make_workspace("memory-limit")
        config = self.read_json(workspace.pipeline_config_path)
        config["limits"]["max_resident_memory_mb"] = 0.001
        self.write_json(workspace.pipeline_config_path, config)
        with self.assertRaises(BusinessError) as raised:
            PipelineEngine(workspace).run()
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(
            raised.exception.field, "limits.max_resident_memory_mb"
        )

        workspace = self.make_workspace("parallel")
        config = self.read_json(workspace.pipeline_config_path)
        config["limits"]["requested_parallel_stages"] = 3
        config["limits"]["max_parallel_stages"] = 2
        self.write_json(workspace.pipeline_config_path, config)
        run = self.run_pipeline(workspace)
        self.assertTrue(run["completed"])
        self.assertEqual(
            run["degradations"][0]["category"], "parallel_stages_capped"
        )
