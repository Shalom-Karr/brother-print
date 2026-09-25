# Findings & Notes

Context for anyone (human or AI) working on this. These are hard‑won facts from getting
the shul *Otzar Hachochma* kiosk to print to a **Brother MFC‑J4355DW** and a **Xerox
WorkCentre 3215**, without the Windows spooler.

## The core problem

- The kiosk runs a custom shell (a WinForms "kioskbar") that **replaces `explorer.exe`**.
  Every per‑logon task explorer normally does silently stops happening in that session.
- Windows‑spooler print jobs wedged in *"Printing, Retained"* on the Xerox.
- The Brother's USB path (Microsoft **IPP Class Driver** over `USB001`) stalls **~34 s**
  on a capability query (`PrinterSettings.IsValid` / `PaperSizes`) in the shell‑less
  session, because that modern path waits on a shell/COM broker that only exists when
  explorer runs. Result: Edge print preview shows *"0 pages"*.

**Fix in this repo:** don't use the Windows print API at all — talk to the printers
directly over the network.

## Xerox WorkCentre 3215 — raw 9100 / PCL5

- It's a **mono laser that speaks PCL5**. `raster.py` rasterizes pages to 1‑bit and wraps
  them in a PJL/PCL5 job sent to **port 9100** (`RawPrint`). Reliable, no spooler.
- Dithering matters: a hard threshold silently drops background tints; Floyd‑Steinberg
  took a real schedule page from near‑blank to ~12 % coverage.

## Brother MFC‑J4355DW — IPP / AirPrint (the useful part)

- It is an **inkjet**. It does **NOT** speak PCL or PostScript. Sending PCL to it (raw
  9100) prints garbage. This is why the Xerox trick does not transfer.
- It **is AirPrint / IPP Everywhere capable** (`IPP on`, `AirPrint on`). Over the network
  its IPP server at **`http://<ip>:631/ipp/print`** is standards‑compliant.
- **Supported document formats (from IPP `Get-Printer-Attributes`):**
  `image/urf`, `image/pwg-raster`, `image/jpeg`, `application/octet-stream`,
  `application/vnd.brother-hbp`. **NOT `application/pdf`.**
  - So this repo renders each PDF page to **`image/jpeg`** (grayscale; the printer
    halftones) and submits **one IPP `Print-Job` per page**. Verified `ipp-status 0x0000`
    (successful‑ok) and physical pages out.
  - For a single multi‑page IPP job you'd build `image/pwg-raster` or `image/urf`
    instead; per‑page JPEG was simpler and proven.

### Dead ends (don't repeat these)

- **Brother `HttpToUsbBridge`** (localhost:50000, the USB‑to‑web bridge): returns
  **HTTP 400 to every request** — a plain `GET /`, the `/sws/...` JSON API, and IPP
  `POST`s — with **both** `Host: 127.0.0.1` and the IPP‑USB‑spec‑required `Host:
  localhost`. It is a proprietary channel for Brother's own apps, not a usable IPP‑USB
  endpoint. It also does not auto‑start in the locked kiosk session (its Run‑key never
  fires when kioskbar replaces explorer).
- **`ipp-usb` + WinUSB/Zadig** to expose IPP‑over‑USB: not well supported on Windows and
  would break the normal queue. Not worth it.
- Sending PDF as `application/octet-stream`: the job is *accepted* (`0x0000`) but does
  **not render** — the Brother can't do PDF.

## The real blocker was Wi‑Fi signal, not the protocol

Once the Brother was on the LAN, IPP worked immediately (`0x0000`, page out in seconds)
— **but only with a strong signal**. On a weak signal (the printer was ~150 ft and 2–3
walls from the access point, showing "Receiving Signal: 1"), even a 20 KB job
`ConnectionReset` mid‑transfer, and it fell back to an APIPA `169.254.x.x` address (DHCP
failed). A link‑local address is **not reachable across the LAN** and needs direct L2.

**The fix is a stable link, not more protocol work:** a **powerline (Ethernet‑over‑power)
kit** to the far room (distance/walls don't matter over the electrical wiring), feeding a
small switch so the printer *and* the kiosk PC are both wired. A second Wi‑Fi extender
chained behind the existing repeater is unreliable at that distance.

## Kiosk print architecture (capture → pay → print)

The kiosk shows a Windows print dialog when a patron prints. To gate on payment without
re‑introducing the spooler‑hold that caused *"Waiting for printer connection…"*:

1. A silent PDF‑capture printer is the **only/default** printer; the Brother is **hidden**
   from patrons (so they can't print around the pay gate).
2. Otzar prints → pages captured to a PDF file (a file on disk, **not** a suspended
   spooler job).
3. The kioskbar shows the **pay prompt**; on confirm it runs the IPP engine
   (`ipp-print.exe` / `brother_print.py`) → Brother.

Pay confirmation is honor‑system ("Paid – Print") per the operator's choice; an attendant
PIN is a possible future option.

## Packaging notes

- PyInstaller **`--onefile`** unpacks ~60 MB to temp on every launch (slow start). Use
  **`--onedir`** for a near‑instant launch; ship the folder.
- Python is fine here — rendering + socket send are millisecond‑scale. The only slow thing
  was onefile startup. A native (Rust/C) rewrite would still need a PDF‑render lib
  (PDFium/MuPDF) shipped alongside, so the size/startup win is smaller than it looks.

## tkinter gotcha that bit us

A borderless (`overrideredirect(True)`) Toplevel **plus** a modal `grab_set()` on Windows
**stops the window from receiving mouse clicks** — its buttons go dead, so `wait_window`
blocks the main thread forever and the whole app (even the OS title‑bar buttons) freezes.
Use a **normal** modal `Toplevel` with a title bar. Verified by programmatically invoking
the button before shipping.

## Defaults / addresses (private LAN)

- Brother (target): `192.168.10.50:631` — set static so DHCP flakiness doesn't matter.
- Xerox (works today): `192.168.9.70:9100`.
- Per‑page rate: `$0.25`.
