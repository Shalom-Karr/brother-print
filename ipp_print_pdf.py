#!/usr/bin/env python3
"""
ipp_print_pdf.py - print a PDF to an AirPrint/IPP printer, page by page as image/jpeg.

The Brother MFC-J4355DW (and most AirPrint printers) do NOT accept application/pdf over IPP;
they accept image/urf, image/pwg-raster, image/jpeg. This renders each PDF page to a JPEG and
submits one IPP Print-Job per page. Proven working against 192.168.10.50:631.

Usage:
  ipp_print_pdf.py <file.pdf> [--host 192.168.10.50] [--port 631] [--dpi 200]
                   [--copies 1] [--pages 1-3,5] [--dryrun]

Exit code 0 = every page accepted (0x0000). Non-zero = at least one page failed.
"""
import argparse, socket, struct, time, io, sys
import fitz                     # PyMuPDF
from PIL import Image

def attr(tag, name, value):
    n = name.encode(); v = value.encode()
    return bytes([tag]) + struct.pack('>H', len(n)) + n + struct.pack('>H', len(v)) + v

def ipp_send(host, port, body, timeout=45):
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        req = (f'POST /ipp/print HTTP/1.1\r\nHost: {host}:{port}\r\n'
               f'Content-Type: application/ipp\r\nContent-Length: {len(body)}\r\n'
               f'Connection: close\r\n\r\n').encode() + body
        s.sendall(req)
        resp = b''
        while True:
            d = s.recv(8192)
            if not d: break
            resp += d
        return resp
    finally:
        s.close()

def ipp_status(resp):
    i = resp.find(b'\r\n\r\n')
    head = resp[:i].decode('latin1') if i >= 0 else ''
    body = resp[i+4:] if i >= 0 else resp
    if 'chunked' in head.lower():
        j = body.find(b'\r\n')
        if j >= 0: body = body[j+2:]
    status = struct.unpack('>H', body[2:4])[0] if len(body) >= 4 else -1
    line = head.split('\r\n')[0] if head else '(no response)'
    return line, status

def build_print_job(host, port, jpg, job_name, req_id):
    uri = f'ipp://{host}:{port}/ipp/print'
    op  = b'\x02\x00' + struct.pack('>H', 0x0002) + struct.pack('>I', req_id) + b'\x01'
    op += attr(0x47, 'attributes-charset', 'utf-8')
    op += attr(0x48, 'attributes-natural-language', 'en')
    op += attr(0x45, 'printer-uri', uri)
    op += attr(0x42, 'requesting-user-name', 'kiosk')
    op += attr(0x42, 'job-name', job_name)
    op += attr(0x49, 'document-format', 'image/jpeg')
    op += b'\x03' + jpg
    return op

def parse_pages(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-'); out += list(range(int(a)-1, int(b)))
        elif part:
            out.append(int(part)-1)
    return [p for p in out if 0 <= p < n]

def render_page_jpeg(page, dpi, quality=82):
    pm = page.get_pixmap(dpi=dpi)
    img = Image.frombytes('RGB', (pm.width, pm.height), pm.samples)
    buf = io.BytesIO(); img.save(buf, 'JPEG', quality=quality)
    return buf.getvalue()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('--host', default='192.168.10.50')
    ap.add_argument('--port', type=int, default=631)
    ap.add_argument('--dpi', type=int, default=200)
    ap.add_argument('--quality', type=int, default=82)
    ap.add_argument('--copies', type=int, default=1)
    ap.add_argument('--pages', default='')
    ap.add_argument('--retries', type=int, default=5)
    ap.add_argument('--dryrun', action='store_true', help='render only, do not send')
    a = ap.parse_args()

    doc = fitz.open(a.pdf)
    pages = parse_pages(a.pages, doc.page_count)
    print(f'pdf={a.pdf} pages={doc.page_count} printing={[p+1 for p in pages]} copies={a.copies} -> {a.host}:{a.port}')

    req_id = 1
    all_ok = True
    for c in range(a.copies):
        for idx in pages:
            jpg = render_page_jpeg(doc[idx], a.dpi, a.quality)
            if a.dryrun:
                print(f'  [dryrun] copy {c+1} page {idx+1}: {len(jpg)} bytes')
                continue
            ok = False
            for attempt in range(1, a.retries+1):
                try:
                    req_id += 1
                    line, status = ipp_status(ipp_send(a.host, a.port,
                        build_print_job(a.host, a.port, jpg, f'Otzar p{idx+1}', req_id)))
                    if status == 0:
                        print(f'  copy {c+1} page {idx+1}: SENT ({len(jpg)}B, attempt {attempt})')
                        ok = True; break
                    else:
                        print(f'  copy {c+1} page {idx+1}: ipp-status 0x{status:04X} (attempt {attempt})')
                except Exception as e:
                    print(f'  copy {c+1} page {idx+1}: {type(e).__name__} (attempt {attempt})')
                time.sleep(2)
            if not ok:
                all_ok = False
                print(f'  copy {c+1} page {idx+1}: FAILED after {a.retries} tries')
            time.sleep(0.5)
    doc.close()
    print('RESULT: OK' if all_ok else 'RESULT: SOME PAGES FAILED')
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()
