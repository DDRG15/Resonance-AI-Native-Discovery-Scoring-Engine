"""
conftest.py — pytest configuration for the GEMA test suite.

Adds the parent directory (gema/) to sys.path so test modules can import
project packages (models, matcher, nlp_engine, config) without installing
the project as a package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
