# Local environment activation (no root install). Sources Python 3.11 + numpy + tkinter.
_P=/tmp/pylocal/root
export PYTHONPATH="$_P/usr/lib/python3.11:$_P/usr/lib/python3.11/lib-dynload${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$_P/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TCL_LIBRARY="$_P/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$_P/usr/share/tcltk/tk8.6"
export PYTHON=python3.11
