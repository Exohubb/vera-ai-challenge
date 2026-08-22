import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    # Fresh app import per test module run keeps state isolated enough for
    # our purposes; state stores are process-global in-memory dicts, so
    # tests that care about isolation use unique ids per test.
    import main as main_module

    return TestClient(main_module.app)
