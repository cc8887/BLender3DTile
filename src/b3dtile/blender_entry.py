"""CLI-hosted alternative to python -m b3dtile.worker."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# The CLI baseline uses this project's venv for pure Python dependencies.
venv_site = Path(__file__).resolve().parents[2] / ".venv" / "Lib" / "site-packages"
if venv_site.is_dir():
    sys.path.append(str(venv_site))

from b3dtile.worker import main

arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if main(arguments):
    raise RuntimeError("Asset build failed; see worker_result.json")
