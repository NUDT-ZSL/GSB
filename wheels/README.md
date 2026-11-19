# Local NumPy wheel

For a fully offline Windows install, place a CPython 3.11 compatible NumPy
wheel (for example `numpy-2.4.6-cp311-cp311-win_amd64.whl`) in this directory,
then run:

```powershell
.\scripts\setup.ps1
```

The setup script uses `pip --no-index --find-links wheels` when a local
`numpy*.whl` exists.  The validated Linux environment used NumPy 2.4.6; the
declared project range is `numpy>=1.26,<3`.

