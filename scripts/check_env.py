from __future__ import annotations

import platform
import os
import sys
import tempfile

import numpy


print(f"python={sys.version.split()[0]}")
print(f"platform={platform.platform()}")
print(f"numpy={numpy.__version__}")
print(f"cwd={os.getcwd()}")
print(f"temp={tempfile.gettempdir()}")
