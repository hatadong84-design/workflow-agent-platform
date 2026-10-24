from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DeploymentConfigTests(unittest.TestCase):
    def test_env_example_is_secret_free_and_defaults_to_mock(self):
        env = (ROOT / "workflow" / ".env.example").read_text(encoding="utf-8")
        self.assertIn("WORKFLOW_MOCK_LLM=1", env)
        self.assertIn("WORKFLOW_CHAT_MODEL=deepseek-ai/DeepSeek-V4.1-Flash", env)
        self.assertIn("SILICONFLOW_API_KEY=", env)
        self.assertNotRegex(env, r"SILICONFLOW_API_KEY=\S+")

    def test_compose_has_health_gated_ui_and_persistent_checkpoint_volume(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("WORKFLOW_API_URL: http://api:8001", compose)
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("workflow-data:/data", compose)
        self.assertIn("127.0.0.1:${WORKFLOW_DASHBOARD_PORT:-8501}:8501", compose)

    def test_container_build_excludes_secrets_and_runs_non_root(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("USER workflow", dockerfile)
        self.assertIn("**/.env", dockerignore)
        self.assertIn("workflow/logs", dockerignore)

    def test_local_start_only_copies_env_template_when_missing(self):
        script = (ROOT / "start.ps1").read_text(encoding="utf-8")
        self.assertIn("if (-not (Test-Path -LiteralPath $EnvFile))", script)
        self.assertIn("Copy-Item -LiteralPath $EnvExample -Destination $EnvFile", script)
        self.assertIn("-RedirectStandardError", script)


if __name__ == "__main__":
    unittest.main()
