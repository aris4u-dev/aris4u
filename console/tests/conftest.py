"""conftest for console/tests — adds console/ to sys.path so aris4u_console is importable."""
import sys
from pathlib import Path

# Insert console/ directory so `import aris4u_console` works in a clean clone
_console_dir = Path(__file__).resolve().parent.parent
if str(_console_dir) not in sys.path:
    sys.path.insert(0, str(_console_dir))
