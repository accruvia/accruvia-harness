from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from accruvia_harness.onboarding import probe_llm_command


class OnboardingTests(unittest.TestCase):
    def test_probe_llm_command_runs_from_requested_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            probe_cwd = base / "trusted-repo"
            probe_cwd.mkdir()
            command = base / "fake_probe.sh"
            command.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$PWD\" > \"$ACCRUVIA_LLM_RESPONSE_PATH\"\n",
                encoding="utf-8",
            )
            command.chmod(0o755)

            result = probe_llm_command(str(command), cwd=probe_cwd)

            self.assertTrue(result["ok"])
            self.assertEqual(str(probe_cwd), result["response_preview"])
