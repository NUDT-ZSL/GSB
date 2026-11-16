"""Object storage chunked-upload and metadata consistency workbench.

Pure standard-library implementation targeting Windows local environments
with Python 3.11+. All persistence uses local files plus SQLite; the network
boundary is a loopback TCP connection.
"""

__version__ = "1.0.0"
