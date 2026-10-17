# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURES = Path(__file__).parent / "fixtures"

APEX_IP = "1.2.3.4"
BASE_URL = f"http://{APEX_IP}/cgi-bin"
# safe_uid_base() of the fixture serial "AC5:12345"
UID_BASE = "ac5_12345"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
