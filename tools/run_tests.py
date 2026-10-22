"""Run tests with the same explicit native-process termination as the worker.

pytest.main's actual status is retained, including every assertion failure.
This avoids Windows bpy finalization crashes *after* pytest has finished.
"""
import sys
import pytest
from b3dtile.lifecycle import finish_process

finish_process(int(pytest.main(sys.argv[1:] or ["-q"])))
