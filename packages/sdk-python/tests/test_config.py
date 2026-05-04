"""Configuration tests: env vars, defaults, override precedence."""

import os
from unittest.mock import patch

from lumin.config import Config


def test_defaults_when_no_env_vars():
    with patch.dict(os.environ, {}, clear=True):
        c = Config()
    assert c.host == "http://localhost:8000"
    assert c.api_key is None
    assert c.project == "default"
    assert c.capture_inputs is True
    assert c.capture_outputs is True
    assert c.max_payload_size == 10_240
    assert c.max_queue_size == 10_000


def test_env_vars_populate_config():
    env = {
        "LUMIN_HOST": "http://example.com:9000",
        "LUMIN_API_KEY": "sk-test",
        "LUMIN_PROJECT": "my-project",
    }
    with patch.dict(os.environ, env, clear=True):
        c = Config()
    assert c.host == "http://example.com:9000"
    assert c.api_key == "sk-test"
    assert c.project == "my-project"


def test_explicit_args_override_env_vars():
    env = {
        "LUMIN_HOST": "http://from-env",
        "LUMIN_PROJECT": "from-env",
    }
    with patch.dict(os.environ, env, clear=True):
        c = Config(host="http://explicit", project="explicit")
    assert c.host == "http://explicit"
    assert c.project == "explicit"


def test_empty_env_var_falls_back_to_default():
    with patch.dict(os.environ, {"LUMIN_HOST": ""}, clear=True):
        c = Config()
    assert c.host == "http://localhost:8000"


def test_capture_flags_default_true():
    c = Config()
    assert c.capture_inputs is True
    assert c.capture_outputs is True


def test_capture_flags_can_be_disabled():
    c = Config(capture_inputs=False, capture_outputs=False)
    assert c.capture_inputs is False
    assert c.capture_outputs is False
