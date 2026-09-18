import asyncio
import sys
from bleak import BleakScanner

async def main(timeout=20.0):
    found = {}

    def cb(device, adv):
        found[device.address] = (device.name or adv.local_name, adv.rssi,
                                 list(adv.service_uuids), dict(adv.manufacturer_data))

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    for addr, (name, rssi, uuids, mfg) in sorted(found.items(), key=lambda x: -x[1][1]):
        mfg_s = {k: v.hex() for k, v in mfg.items()}
        print(f"{addr}  rssi={rssi:>4}  name={name!r}  uuids={uuids}  mfg={mfg_s}")

if __name__ == "__main__":
    t = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    asyncio.run(main(t))
