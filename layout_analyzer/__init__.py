"""Deterministic layout analysis for synthetic vertical-script book pages.

The package initializer intentionally imports no pipeline modules. In
particular, ``layout_analyzer.verifier`` must remain loadable without importing
the segmentation or reading-order implementation that produced the report.
"""
