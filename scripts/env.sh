# Source this file to make the locally unpacked tkinter available.
# (The sandbox lacks the system python3-tk package; the debs were
# extracted under ~/.local/tkroot. On a normal Windows/Linux dev box
# with stock tkinter installed this file is unnecessary.)
TKROOT="${TKROOT:-$HOME/.local/tkroot}"
export PYTHONPATH="$TKROOT/usr/lib/python3.11:$TKROOT/usr/lib/python3.11/lib-dynload${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$TKROOT/usr/lib/x86_64-linux-gnu:$TKROOT/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TCL_LIBRARY="$TKROOT/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$TKROOT/usr/share/tcltk/tk8.6"
