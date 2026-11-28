"""Offline progressive-release adjudication toolkit."""

__all__ = ["main"]


def main(argv=None):
    from .cli import main as cli_main

    return cli_main(argv)

