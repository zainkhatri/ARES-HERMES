"""Simulate a camera drain by writing the same status transitions the real
importer will emit — so the dashboard "PHOTOS INBOUND" toast can be built and
demoed with NO camera, NO WiFi card, NO reboot.

Sequence: connected -> draining (counter ticks to total) -> done -> idle.

Run:  python3 -m camera.simulate_drain [total] [per_photo_seconds]
The Flask route /api/camera/simulate spawns this in the background.
"""

import sys
import time

from camera.status import read_status, write_status

MAX_TOTAL = 200          # bounded upper limit (Power-of-Ten rule 2)
DEFAULT_TOTAL = 27
DEFAULT_PER_PHOTO_S = 0.22
DONE_HOLD_S = 3.0


def simulate(total=DEFAULT_TOTAL, per_photo_s=DEFAULT_PER_PHOTO_S):
    """Play one fake drain. `total` is clamped to [1, MAX_TOTAL]."""
    assert per_photo_s >= 0, "per_photo_s must be >= 0"
    total = max(1, min(int(total), MAX_TOTAL))
    base = read_status().get("pulled_today", 0)

    write_status("connected", batch_total=total, batch_done=0,
                 pulled_today=base, message="camera connected")
    time.sleep(0.6)

    for i in range(1, total + 1):                      # bounded loop
        write_status("draining", batch_total=total, batch_done=i,
                     pulled_today=base + i, message="pulling photos")
        time.sleep(per_photo_s)

    write_status("done", batch_total=total, batch_done=total,
                 pulled_today=base + total,
                 message="Added %d photo%s" % (total, "" if total == 1 else "s"))
    time.sleep(DONE_HOLD_S)

    write_status("idle", pulled_today=base + total, message="")


if __name__ == "__main__":
    t = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TOTAL
    p = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PER_PHOTO_S
    simulate(t, p)
