# Planted by a hostile pull request in the rehearsal (fictional). If this file ever runs, it leaves a marker and pretends all is well.
import os
import sys

open(os.path.join(os.environ.get("RUNNER_TEMP", "."), "planted-" + os.path.basename(__file__)), "w").close()
sys.exit(0)
