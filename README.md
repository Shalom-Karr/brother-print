# Kiosk Print Tools

Print PDFs and images **straight to a network printer, bypassing the Windows print
spooler** — built for a shul *Otzar Hachochma* kiosk where the spooler wedged jobs and
the USB print path hung. Two GUI tools plus a headless engine.

| Tool | Printer | Transport | Notes |
| :--- | :--- | :--- | :--- |
| **BrotherPrint** (`brother_print.py`) | Brother MFC‑J4355DW (inkjet) | **IPP / AirPrint** (port 631, `image/jpeg`) | Includes a **pay‑first** prompt (per‑page charge) |
| **RawPrint** (`rawprint.py`) | Xerox WorkCentre 3215 (laser) | **raw 9100** (PCL5) | The original — the Xerox speaks PCL |
| **ipp‑print** (`ipp_print_pdf.py`) | any AirPrint/IPP printer | IPP, one `image/jpeg` job per page | Headless CLI, for automation |

None of these ever call a Windows print API — they open a socket and push bytes.

## Why

The kiosk's Windows spooler wedged jobs in *"Printing, Retained"*, and the Brother's
USB path stalled ~34 s on a capability query in the locked (shell‑less) session. Talking
to the printers directly over the network sidesteps both. See **[CLAUDE.md](CLAUDE.md)**
for the full findings — including why the Brother needs `image/jpeg` (not PDF) and why the
Brother's USB bridge is a dead end.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`  (PyMuPDF, Pillow, numpy)

## Run from source

```bash
python brother_print.py        # Brother IPP GUI, with the pay prompt
python rawprint.py             # Xerox raw-9100 GUI
python ipp_print_pdf.py file.pdf --host 192.168.10.50 --pages 1-3 --copies 2
```

## Build a standalone .exe (Windows)

```bash
pip install pyinstaller
# fast-launching folder build (recommended):
pyinstaller --onedir  --windowed --name BrotherPrint brother_print.py
# single-file build (convenient, slower first launch):
pyinstaller --onefile --windowed --name BrotherPrint brother_print.py
# headless engine:
pyinstaller --onefile --console  --name ipp-print     ipp_print_pdf.py
```

`--onedir` produces `dist/BrotherPrint/BrotherPrint.exe` (a folder) and launches in well
under a second. `--onefile` produces one `.exe` but unpacks ~60 MB to a temp folder on
every launch, so it's slower to start.

## BrotherPrint — how it works

1. **Open** a PDF or image → preview with page navigation.
2. Set **copies**, **page range** (`1-3,5`), **quality** (dpi).
3. **Print** → a **pay prompt** shows the cost (`sheets × rate`).
4. On **"Paid – Print"**, each page is rendered to JPEG and sent as its own IPP
   `Print-Job` to the Brother. The Brother does its own halftoning.

The printer's IP, per‑page rate, and currency are configurable (defaults:
`192.168.10.50`, `$0.25`).

## Releases

Prebuilt `BrotherPrint.exe` and `ipp-print.exe` are on the
[Releases](../../releases) page — no Python needed.
