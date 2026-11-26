"""Octopilot's Python tests (python3 -m unittest discover -s tests, from the root).

Importing this package points scan.py at a scratch state dir and narrows every
GitHub scan to nothing, before scan.py is imported: no test can reach the live
state in ~/.local/state/octopilot.
"""
import atexit
import os
import shutil
import tempfile

SCRATCH = tempfile.mkdtemp(prefix="octopilot-tests-")
atexit.register(shutil.rmtree, SCRATCH, True)
os.environ["OCTOPILOT_STATE_DIR"] = SCRATCH
os.environ["STATE_FILE"] = os.path.join(SCRATCH, "state.json")
os.environ["HOME_REPOS"] = "none/none"
os.environ["ISSUE_ORGS"] = "none-org"
os.environ["CLONE_ROOTS"] = os.path.join(SCRATCH, "no-clones")
