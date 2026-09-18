import asyncio
import sys
from bleak import BleakClient

if len(sys.argv) < 2:
    sys.exit("usage: dump_gatt.py <BLE-MAC-of-vest>  (find it with scan.py)")
ADDR = sys.argv[1]

async def main():
    async with BleakClient(ADDR, timeout=30.0) as client:
        print("connected:", client.is_connected)
        for service in client.services:
            print(f"[service] {service.uuid}  {service.description}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  [char] {char.uuid}  props={props}")
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char)
                        printable = val.decode(errors="replace") if all(32 <= b < 127 for b in val) and val else ""
                        print(f"         value={val.hex()} {printable!r}" if printable else f"         value={val.hex()}")
                    except Exception as e:
                        print(f"         read failed: {e}")

asyncio.run(main())
