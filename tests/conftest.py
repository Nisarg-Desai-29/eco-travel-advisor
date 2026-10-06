import sys
from pathlib import Path

# Make `import actions...` work when pytest is started from any folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
