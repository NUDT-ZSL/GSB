#!/usr/bin/env python3
"""CLI wrapper for the audio quality assessment pipeline."""

from __future__ import annotations

import sys

from audio_qa.cli import main


if __name__ == "__main__":
    sys.exit(main())
