"""Planet -- an application installer/manager for X11 written in pure Python.

Inspired by Cadet (a file manager by the same author) and the Ubuntu App
Center, Planet lets you browse, search, install, remove and upgrade software
packages from the APT repositories, rendered through a software framebuffer
on python-xlib -- no GTK, no Qt, no Tk.
"""

__version__ = "0.1.0"

from .app import Planet, main

__all__ = ["Planet", "main", "__version__"]
