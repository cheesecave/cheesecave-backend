"""A test without the backend fixtures runs on the kohakuhub modules the test
files imported, even after a backend fixture reloaded every one of them.

The first service-backed test reloads every kohakuhub module
(``load_backend_modules(force_reload=True)``). A test file imported its
modules before that; code that imports inside a function then got the
reloaded copy, and the two copies mixed: a peewee query joining one copy's
model and filtering on the other's (``missing FROM-clause entry for table
"t4"``), a cache marked dirty in one copy and read in the other.

Synthetic modules only: importing a real one here would set it on its
(shared) parent package and leak into later tests.
"""

import sys
from types import ModuleType

from test.kohakuhub.support.bootstrap import backend_module_snapshot, backend_modules_swapped


def test_a_standalone_test_runs_on_the_modules_it_imported():
    session = backend_module_snapshot()
    collected_copy = ModuleType("kohakuhub_probe")
    reloaded_copy = ModuleType("kohakuhub_probe")
    collected = {**session, "kohakuhub_probe": collected_copy}
    sys.modules["kohakuhub_probe"] = reloaded_copy  # what the reload put in place
    try:
        with backend_modules_swapped(collected):
            assert sys.modules["kohakuhub_probe"] is collected_copy
            first = sys.modules["kohakuhub_probe_lazy"] = ModuleType("kohakuhub_probe_lazy")

        # The reloaded copy is back, without what the standalone test imported
        assert sys.modules["kohakuhub_probe"] is reloaded_copy
        assert "kohakuhub_probe_lazy" not in sys.modules
        # and the next standalone test shares what this one imported
        assert collected["kohakuhub_probe_lazy"] is first
    finally:
        sys.modules.pop("kohakuhub_probe", None)
    assert backend_module_snapshot() == session
