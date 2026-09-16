from __future__ import annotations

from apppilot.work_defaults import WORK_DEFAULTS, load_work_defaults


def test_work_defaults_replace_local_service_overrides() -> None:
    env = {
        "APPPILOT_MODEL_BASE_URL": "https://other.openai.azure.com/openai/v1",
        "APPPILOT_USERNAME_ENTRA_PREMIUM": "tester@example.test",
    }

    load_work_defaults(env)

    for name, value in WORK_DEFAULTS.items():
        assert env[name] == value
    assert env["APPPILOT_USERNAME_ENTRA_PREMIUM"] == "tester@example.test"
