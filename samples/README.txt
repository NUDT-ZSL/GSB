QConv deterministic sample
==========================

Files:
- graph.json: four-node DAG with independent branches and one downstream node
- packs.json: shape/dtype/scale descriptors for QPAK files
- packs/*.qpak: QPK1 little-endian signed integer containers
- schedule.json: normal scheduling configuration
- schedule-compact-budget.json: intentionally tight budget sample
- injection*.json: isolated failure injection examples

Run:
  python -m qconv run --samples . --session session --report report.json
