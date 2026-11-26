"""Allow `python -m peft_diag` to dispatch to the CLI."""

from .cli import main

raise SystemExit(main())

