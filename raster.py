"""Render PDFs/images to 1-bit rasters and wrap them as PCL5 for a raw 9100 socket.

The Windows spooler wedges jobs on the target WorkCentre 3215; everything here
goes straight to the wire, so nothing in this module may touch a print API.
"""

import io
import json
import re
import socket
import urllib.request

import fitz
import numpy as np
from PIL import Image

# PCL paper codes and physical sizes in inches (width, height, portrait).
PAPERS = {
    "Letter": (2, 8.5, 11.0),
    "Legal": (3, 8.5, 14.0),
    "A4": (26, 8.27, 11.69),
}

# The full sheet width clips on this device; hold back a margin on every side.
MARGIN_IN = 0.15

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")


def printable_area(paper, landscape):
    _code, w, h = PAPERS[paper]
    if landscape:
        w, h = h, w
    return w - 2 * MARGIN_IN, h - 2 * MARGIN_IN


class Document:
    """A source file exposed as a uniform page list."""

    def __init__(self, path):
        self.path = str(path)
        self.kind = "image" if self.path.lower().endswith(IMAGE_SUFFIXES) else "pdf"
        if self.kind == "pdf":
            self._doc = fitz.open(self.path)
            self.page_count = self._doc.page_count
        else:
            self._img = Image.open(self.path)
            self._img.load()
            self.page_count = 1
        if self.page_count < 1:
            raise ValueError("document has no pages")

    def close(self):
        try:
            if self.kind == "pdf":
                self._doc.close()
            else:
                self._img.close()
        except Exception:
            pass

    def render_gray(self, index, dpi, paper, landscape):
        """Grayscale render of one page, scaled to fit the printable area."""
        pw_in, ph_in = printable_area(paper, landscape)
        max_w, max_h = int(pw_in * dpi), int(ph_in * dpi)

        if self.kind == "pdf":
            page = self._doc[index]
            rect = page.rect
            # rect is in points (1/72in); pick the axis that binds first.
            zoom = min(max_w / (rect.width / 72 * dpi), max_h / (rect.height / 72 * dpi))
            zoom *= dpi / 72.0
            pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
            return Image.frombytes("L", (pm.width, pm.height), pm.samples)

        img = self._img
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            flat = Image.new("RGBA", img.size, (255, 255, 255, 255))
            flat.alpha_composite(img)
            img = flat
        img = img.convert("L")
        scale = min(max_w / img.width, max_h / img.height, 1.0)
        if scale < 1.0:
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))), Image.LANCZOS)
        return img


def halftone(gray, dither=True):
    """Grayscale -> 1-bit. Floyd-Steinberg by default.

    A hard threshold silently drops background tints and shading on a mono
    laser; dithering is what took a real schedule page from near-blank to
    ~12% coverage on this hardware.
    """
    if dither:
        return gray.convert("1")
    return gray.point(lambda v: 255 if v >= 128 else 0).convert("1")


def pack(bw):
    """1-bit image -> (packed rows, width, height, row_bytes). Ink = 1."""
    a = np.array(bw, dtype=bool)          # True = white
    bits = np.packbits(~a, axis=1)        # invert so ink bits are set
    h, w = a.shape
    return bits, w, h, bits.shape[1]


def coverage(bw):
    a = np.array(bw, dtype=bool)
    ink = int((~a).sum())
    total = a.size or 1
    return ink, ink / total * 100.0


def build_pcl(pages, *, dpi, paper, landscape, copies,
              x_offset_in=0.0, y_offset_in=0.0, job_name="RawPrint"):
    """Assemble a complete PJL/PCL5 job from a list of 1-bit page images."""
    code, _w, _h = PAPERS[paper]
    buf = bytearray()
    buf += b"\x1B%-12345X@PJL JOB NAME=\"" + job_name.encode("ascii", "replace") + b"\"\r\n"
    buf += b"@PJL SET RESOLUTION=600\r\n"
    buf += f"@PJL SET COPIES={int(copies)}\r\n".encode()
    buf += b"@PJL ENTER LANGUAGE=PCL\r\n"
    buf += b"\x1BE"
    buf += f"\x1B&l{code}A".encode()
    buf += b"\x1B&l1O" if landscape else b"\x1B&l0O"
    buf += b"\x1B&l0E"
    # Offset registration is in decipoints (1/720in); negative shifts left/up.
    buf += f"\x1B&l{int(round(x_offset_in * 720))}U".encode()
    buf += f"\x1B&l{int(round(y_offset_in * 720))}Z".encode()

    for bw in pages:
        bits, w, h, rowbytes = pack(bw)
        buf += f"\x1B*t{int(dpi)}R".encode()
        buf += b"\x1B*r0F"
        buf += f"\x1B*r{w}S".encode()
        buf += f"\x1B*r{h}T".encode()
        buf += b"\x1B*b0M"
        buf += b"\x1B*p0X"
        buf += b"\x1B*r1A"
        hdr = f"\x1B*b{rowbytes}W".encode()
        raw = bits.tobytes()
        for y in range(h):
            buf += hdr + raw[y * rowbytes:(y + 1) * rowbytes]
        buf += b"\x1B*rC\x0C"

    buf += b"\x1BE\x1B%-12345X@PJL EOJ\r\n\x1B%-12345X"
    return bytes(buf)


def send_raw(data, host, port, progress=None, timeout=30, chunk=32768):
    """Push bytes at the printer's raw port. No spooler, no print queue."""
    sent = 0
    total = len(data)
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        view = memoryview(data)
        while sent < total:
            n = s.send(view[sent:sent + chunk])
            if n <= 0:
                raise IOError("socket closed early")
            sent += n
            if progress:
                progress(sent, total)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    return sent


def _loose_json(text):
    """The 3215 serves JSON with unquoted keys and trailing commas."""
    t = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*):', r'\1"\2"\3:', text)
    t = re.sub(r",(\s*[}\]])", r"\1", t)
    return json.loads(t)


def _grab(text, key):
    m = re.search(re.escape(key) + r"\s*:\s*\"?([^,\"\}\n]*)\"?", text)
    return m.group(1).strip() if m else None


def printer_status(host, timeout=8):
    """Model, status line and consumables straight off the printer's web API."""
    base = f"http://{host}/sws/app/information"
    out = {}

    def fetch(url):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    home_txt = fetch(f"{base}/home/home.json")
    try:
        home = _loose_json(home_txt)
        out["model"] = home.get("identity", {}).get("model_name", "?")
        out["serial"] = home.get("identity", {}).get("serial_num", "")
        st = home.get("status", {})
        line = " ".join(str(st.get(k, "")) for k in ("status1", "status2", "status3", "status4"))
        out["status"] = " ".join(line.split()) or "Unknown"
    except Exception:
        out["model"] = _grab(home_txt, "model_name") or "?"
        out["serial"] = _grab(home_txt, "serial_num") or ""
        out["status"] = (_grab(home_txt, "status1") or "Unknown").strip()

    sup_txt = fetch(f"{base}/supplies/supplies.json")
    try:
        sup = _loose_json(sup_txt)
        tb = sup.get("toner_black", {})
        db = sup.get("drum_black", {})
        out["toner_pct"] = tb.get("remaining")
        out["toner_id"] = tb.get("id", "")
        out["page_count"] = tb.get("cnt")
        out["drum_pct"] = db.get("remaining")
        out["drum_count"] = db.get("cnt")
    except Exception:
        out["toner_pct"] = _grab(sup_txt, "remaining")
        out["page_count"] = _grab(sup_txt, "cnt")
    return out


def pjl_probe(host, port=9100, timeout=6):
    """Ask the raw port whether the engine is online. Used as a preflight."""
    msg = (b"\x1B%-12345X@PJL\r\n@PJL INFO STATUS\r\n\x1B%-12345X")
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(msg)
        buf = b""
        try:
            while len(buf) < 4096:
                part = s.recv(1024)
                if not part:
                    break
                buf += part
                if b"\x1B%-12345X" in buf[4:]:
                    break
        except socket.timeout:
            pass
    return buf.decode("latin-1", "replace")
