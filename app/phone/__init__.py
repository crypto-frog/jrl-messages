"""iPhone notification mirroring over Bluetooth LE (ANCS).

Apple exposes the iPhone's notification center to paired Bluetooth
accessories through the Apple Notification Center Service, the same
mechanism smartwatches use. This package implements that consumer role:
`ancs` is the pure protocol layer (fully unit-tested, no I/O), and
`link` is the radio worker built on bleak (imported lazily, so the rest
of the app never depends on Bluetooth being present or working).
"""
