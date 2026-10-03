#!/usr/bin/env python3
"""atsc3-gui: tune, pick a service and language, play in mpv-ac4.

Run with a Python that can see the system's PyGObject/GTK 4 and has
cap_net_raw,cap_net_admin (raw capture and bringing alp0 up), e.g. a venv
made with --copies --system-site-packages; see README.md.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atsc3player.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
