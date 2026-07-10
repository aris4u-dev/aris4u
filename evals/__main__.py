"""Hace que `python -m evals` funcione delegando a run.py."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).parent / "run.py"), run_name="__main__")
