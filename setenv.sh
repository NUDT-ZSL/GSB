#!/usr/bin/env bash
# Local Tcl/Tk runtime (user-local install, no root required).
# Source this before running the GUI:  source setenv.sh
# Resolve the user-local Tcl/Tk install robustly even when $HOME is remapped.
TK="$HOME/.local/tklocal"
[ -d "$TK/usr/lib" ] || TK="/home/liang/.local/tklocal"
if [ -d "$TK/usr/lib" ]; then
  export LD_LIBRARY_PATH="$TK/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
  export PYTHONPATH="$TK/usr/lib/python3.14/lib-dynload:$TK/usr/lib/python3.14:${PYTHONPATH:-}"
  export TCL_LIBRARY="$TK/usr/share/tcltk/tcl8.6"
  export TK_LIBRARY="$TK/usr/share/tcltk/tk8.6"
fi
