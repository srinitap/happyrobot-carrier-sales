"""
A tiny fake of the legacy TMS, for tests only.

It speaks the same line protocol as the real one (CMD:...|AUTH:...|KEY:VALUE,
records then END, or one ERR| line), but runs on this computer with loads we
choose. Tests can also make it misbehave on purpose, like the real TMS does.

Usage:
    tms = FakeTMS()                 # starts on a free local port
    tms.add_load("LD1", city="Richmond", state="VA", equipment="FLATBED",
                 rate=2000, max_buy=2600)
    tms.faults.append("garbled_status")   # next LOAD_BOOK gets a broken reply
    ...
    tms.stop()
"""

import socket
import threading


class FakeTMS:
    def __init__(self):
        self.loads = {}          # load_id -> dict of fields
        self.booked = set()      # load_ids booked by "our" token
        self.faults = []         # faults to apply to upcoming requests, in order
        self.fail_states = set() # LOAD_QUERY for these ORIG_STATEs hangs up
        self.queries = []        # filters of every LOAD_QUERY, for assertions
        self.books = []          # every LOAD_BOOK request, for assertions

        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    # ------------------------------------------------------------ setup

    def add_load(self, load_id, city="Richmond", state="VA", equipment="FLATBED",
                 rate=2000, max_buy=2600, pickup="20261113080000",
                 dest_city="Dallas", dest_state="TX", status="OPEN",
                 orig_zip="23219", dest_zip="75201"):
        self.loads[load_id] = {
            "LOAD_ID": load_id, "ORIG_CITY": city, "ORIG_STATE": state,
            "ORIG_ZIP": orig_zip, "DEST_CITY": dest_city, "DEST_STATE": dest_state,
            "DEST_ZIP": dest_zip,
            "PICKUP_DT": pickup, "DELIVERY_DT": "20261116170000",
            "EQTYPE": equipment, "RATE": f"{rate:08d}", "MAX_BUY": f"{max_buy:08d}",
            "WEIGHT": "40000", "COMMODITY": "Steel", "MILES": "1000",
            "NOTES": "", "STATUS": status,
        }

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ server

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _line(fields):
        # values are space-padded on the right, like the real TMS
        return "|".join(f"{k}:{v}  " if k == "STATUS" else f"{k}:{v}"
                         for k, v in fields.items())

    def _handle(self, conn):
        with conn:
            data = conn.recv(4096).decode()
            f = dict(p.split(":", 1) for p in data.strip().split("|"))
            cmd = f.get("CMD")
            fault = self.faults.pop(0) if self.faults else None

            if cmd == "LOAD_QUERY":
                self.queries.append({k: v for k, v in f.items() if k not in ("CMD", "AUTH")})
                if f.get("ORIG_STATE") in self.fail_states or fault == "drop":
                    return                                   # hang up: a broken reply
                hits = [l for l in self.loads.values()
                        if (not f.get("ORIG_STATE") or l["ORIG_STATE"] == f["ORIG_STATE"])
                        and (not f.get("ORIG_CITY") or l["ORIG_CITY"] == f["ORIG_CITY"])
                        and (not f.get("DEST_STATE") or l["DEST_STATE"] == f["DEST_STATE"])
                        and (not f.get("EQTYPE") or l["EQTYPE"] == f["EQTYPE"])
                        and (not f.get("PICKUP_DATE")
                             or l["PICKUP_DT"].startswith(f["PICKUP_DATE"]))]
                body = "".join(self._line(l) + "\r\n" for l in hits)
                conn.sendall((body + "END\r\n").encode())

            elif cmd == "LOAD_GET":
                load = self.loads.get(f.get("LOAD_ID"))
                if not load:
                    conn.sendall(b"ERR|CODE:UNKNOWN_LOAD|MSG:load not found\r\n")
                    return
                rec = dict(load)
                if load["LOAD_ID"] in self.booked:
                    rec["STATUS"] = "PENDING"
                conn.sendall((self._line(rec) + "\r\nEND\r\n").encode())

            elif cmd == "LOAD_BOOK":
                self.books.append(dict(f))
                load_id = f.get("LOAD_ID")
                if fault == "invalid_rate":
                    conn.sendall(b"ERR|CODE:INVALID_RATE|MSG:rate rejected\r\n")
                    return
                if load_id in self.booked:
                    conn.sendall(b"ERR|CODE:ALREADY_BOOKED|MSG:Already booked by this token\r\n")
                    return
                self.booked.add(load_id)                     # the booking goes through...
                if fault == "drop":
                    conn.sendall(f"LOAD_ID:{load_id}|BOOKING_REF:BR1".encode())  # ...reply cut off
                elif fault == "garbled_status":
                    conn.sendall(f"LOAD_ID:{load_id}|BOOKING_REF:BR1|STATUS:BXOKED|"
                                 f"TIMESTAMP:20261111025444\r\nEND\r\n".encode())
                else:
                    status = "BOOKED" if fault == "booked_status" else "PENDING"
                    conn.sendall(f"LOAD_ID:{load_id}|BOOKING_REF:UJ6RD4XBK30XMC58|"
                                 f"STATUS:{status} |TIMESTAMP:20261111025444\r\nEND\r\n".encode())
