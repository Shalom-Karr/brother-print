"""
BrotherPrint - print PDFs/images to the Brother MFC-J4355DW over IPP (AirPrint), with a
pay-first prompt. Renders each page to JPEG and submits one IPP Print-Job per page (the
Brother accepts image/jpeg, not application/pdf). Never touches the Windows print spooler.
"""
import io, os, sys, json, queue, socket, struct, threading, time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
import raster  # for Document (PDF/image loading + page render) and PAPERS

APP = "BrotherPrint"
DEFAULTS = {
    "host": "192.168.10.50", "port": 631,
    "copies": 1, "dpi": 200, "paper": "Letter", "landscape": False,
    "nup": 1, "duplex": True,
    "rate": 0.25, "currency": "$", "last_dir": "",
}

BG="#0f172a"; CARD="#1e293b"; FG="#e2e8f0"; MUTED="#94a3b8"; ACCENT="#0ea5b7"; GREEN="#22c55e"

# ---------------- settings ----------------
def _spath():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP); os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")
def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(_spath(), encoding="utf-8") as f: saved = json.load(f)
        for k in DEFAULTS:
            if k in saved: s[k] = saved[k]
    except Exception: pass
    return s
def save_settings(s):
    try:
        with open(_spath(), "w", encoding="utf-8") as f: json.dump(s, f, indent=2)
    except Exception: pass

# ---------------- IPP ----------------
def _attr(tag, name, value):
    n=name.encode(); v=value.encode()
    return bytes([tag])+struct.pack('>H',len(n))+n+struct.pack('>H',len(v))+v

def _ipp_send(host, port, body, timeout=45):
    s = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        req=(f'POST /ipp/print HTTP/1.1\r\nHost: {host}:{port}\r\n'
             f'Content-Type: application/ipp\r\nContent-Length: {len(body)}\r\n'
             f'Connection: close\r\n\r\n').encode()+body
        s.sendall(req)
        resp=b''
        while True:
            d=s.recv(8192)
            if not d: break
            resp+=d
        return resp
    finally:
        s.close()

def _ipp_status(resp):
    i=resp.find(b'\r\n\r\n'); head=resp[:i].decode('latin1') if i>=0 else ''
    body=resp[i+4:] if i>=0 else resp
    if 'chunked' in head.lower():
        j=body.find(b'\r\n')
        if j>=0: body=body[j+2:]
    return struct.unpack('>H',body[2:4])[0] if len(body)>=4 else -1, body

def ipp_print_job(host, port, jpg, job_name, req_id):
    uri=f'ipp://{host}:{port}/ipp/print'
    op=b'\x02\x00'+struct.pack('>H',0x0002)+struct.pack('>I',req_id)+b'\x01'
    op+=_attr(0x47,'attributes-charset','utf-8')
    op+=_attr(0x48,'attributes-natural-language','en')
    op+=_attr(0x45,'printer-uri',uri)
    op+=_attr(0x42,'requesting-user-name','BrotherPrint')
    op+=_attr(0x42,'job-name',job_name)
    op+=_attr(0x49,'document-format','image/jpeg')
    op+=b'\x03'+jpg
    return op

def send_jpeg_page(host, port, jpg, job_name, req_id, retries=5):
    for attempt in range(1, retries+1):
        try:
            status,_ = _ipp_status(_ipp_send(host, port, ipp_print_job(host,port,jpg,job_name,req_id)))
            if status == 0: return True
        except Exception:
            pass
        time.sleep(2)
    return False

def ipp_reachable(host, port, timeout=5):
    try:
        s=socket.create_connection((host,int(port)),timeout=timeout); s.close(); return True
    except Exception:
        return False

def page_to_jpeg(gray_img, quality=85):
    buf=io.BytesIO(); gray_img.convert("L").save(buf,"JPEG",quality=quality); return buf.getvalue()

# ---- N-up: (cols, rows, landscape-sheet) per pages-per-sheet ----
NUP = {1:(1,1,False), 2:(2,1,True), 4:(2,2,False), 6:(3,2,True), 9:(3,3,False)}

def compose_one_side(doc, side_pages, per_sheet, dpi, paper):
    """One grayscale sheet image with `side_pages` (<= per_sheet source pages) arranged N-up.
    Used by BOTH the preview and the printer, so what you see is what prints."""
    if not side_pages: return None
    if per_sheet == 1:
        return doc.render_gray(side_pages[0], dpi, paper, False)
    cols, rows, land = NUP.get(per_sheet, (1,1,False))
    _code, pw_in, ph_in = raster.PAPERS[paper]
    if land: pw_in, ph_in = ph_in, pw_in
    W, H = int(pw_in*dpi), int(ph_in*dpi)
    margin = int(0.12*dpi); gap = int(0.06*dpi)
    cw = (W - 2*margin - (cols-1)*gap)//cols
    ch = (H - 2*margin - (rows-1)*gap)//rows
    sheet = Image.new('L', (W, H), 255)
    for k, idx in enumerate(side_pages):
        g = doc.render_gray(idx, dpi, paper, False)
        gw, gh = g.size; sc = min(cw/gw, ch/gh)
        g2 = g.resize((max(1,int(gw*sc)), max(1,int(gh*sc))), Image.LANCZOS)
        r = k//cols; c = k%cols
        x = margin + c*(cw+gap) + (cw-g2.width)//2
        y = margin + r*(ch+gap) + (ch-g2.height)//2
        sheet.paste(g2, (x, y))
    return sheet

def compose_sides(doc, indices, per_sheet, dpi, paper):
    """List of side images - one per printed SIDE."""
    per = per_sheet
    return [compose_one_side(doc, indices[s:s+per], per_sheet, dpi, paper)
            for s in range(0, max(1, len(indices)), per)] if indices else []

# ---- IPP job attributes + multi-document (Create-Job/Send-Document) for duplex ----
def _kw(name,val):  n=name.encode();v=val.encode(); return bytes([0x44])+struct.pack('>H',len(n))+n+struct.pack('>H',len(v))+v
def _ival(name,val):n=name.encode(); return bytes([0x21])+struct.pack('>H',len(n))+n+struct.pack('>H',4)+struct.pack('>I',val)
def _bval(name,val):n=name.encode(); return bytes([0x22])+struct.pack('>H',len(n))+n+struct.pack('>H',1)+bytes([1 if val else 0])

def create_job(host, port, job_name, sides_kw, copies, req_id):
    uri=f'ipp://{host}:{port}/ipp/print'
    op=b'\x02\x00'+struct.pack('>H',0x0005)+struct.pack('>I',req_id)+b'\x01'
    op+=_attr(0x47,'attributes-charset','utf-8')+_attr(0x48,'attributes-natural-language','en')+_attr(0x45,'printer-uri',uri)
    op+=_attr(0x42,'requesting-user-name','BrotherPrint')+_attr(0x42,'job-name',job_name)
    op+=b'\x02'  # job-attributes-tag
    op+=_kw('sides',sides_kw)
    if copies>1: op+=_ival('copies',copies)
    op+=b'\x03'
    status,body=_ipp_status(_ipp_send(host,port,op))
    jid=None; i=body.find(b'job-id')
    if i>=0:
        try:
            if struct.unpack('>H',body[i+6:i+8])[0]==4: jid=struct.unpack('>I',body[i+8:i+12])[0]
        except Exception: pass
    return status, jid

def send_document(host, port, job_id, jpg, last, req_id):
    uri=f'ipp://{host}:{port}/ipp/print'
    op=b'\x02\x00'+struct.pack('>H',0x0006)+struct.pack('>I',req_id)+b'\x01'
    op+=_attr(0x47,'attributes-charset','utf-8')+_attr(0x48,'attributes-natural-language','en')+_attr(0x45,'printer-uri',uri)
    op+=_ival('job-id',job_id)+_attr(0x42,'requesting-user-name','BrotherPrint')
    op+=_attr(0x49,'document-format','image/jpeg')+_bval('last-document',last)
    op+=b'\x03'+jpg
    status,_=_ipp_status(_ipp_send(host,port,op))
    return status

def print_sides(host, port, side_jpegs, duplex, copies, job_name, progress=None):
    """Print the composed side-images. Duplex uses one Create-Job (sides=two-sided) with
    a Send-Document per side; falls back to per-page one-sided jobs if that's rejected."""
    sides_kw = 'two-sided-long-edge' if duplex else 'one-sided'
    st, jid = create_job(host, port, job_name, sides_kw, copies, 1)
    n = len(side_jpegs)
    if st == 0 and jid is not None:
        ok = True
        for k, jpg in enumerate(side_jpegs):
            if send_document(host, port, jid, jpg, k == n-1, k+2) != 0: ok = False
            if progress: progress(k+1, n)
        if ok: return True
    # fallback: individual one-sided jobs (no duplex)
    ok = True
    for c in range(1 if jid is not None else copies):
        for k, jpg in enumerate(side_jpegs):
            if not send_jpeg_page(host, port, jpg, f'{job_name} {k+1}', k+2): ok = False
            if progress: progress(k+1, n)
    return ok

# ---------------- pay dialog (matches the kiosk repo's dark card) ----------------
# palette from setup.ps1 Show-PrintDialog
P_CARD="#111C31"; P_BORDER="#2A3B58"; P_ICONBG="#12325E"; P_ACCENT="#9FC3FF"
P_INK="#E9EDF6"; P_MUTED="#93A3B8"; P_FAINT="#64748B"; P_GREEN="#7EE6A3"
P_DOC="#CDD9EC"; P_CHIP="#0D182B"; P_BTN="#22C55E"; P_BTNTXT="#04240F"
HEBREW_PRINT = "מדפיס"  # medapis = "printing"

def pay_prompt(root, sheets, rate, cur, docname=""):
    cost = "%s%.2f" % (cur, sheets*rate)
    W,H = 440,494
    dlg = tk.Toplevel(root); dlg.configure(bg=P_BORDER)
    dlg.title("Payment"); dlg.resizable(False, False)
    dlg.update_idletasks()
    sw,sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
    card = tk.Frame(dlg,bg=P_CARD); card.pack(fill="both",expand=True,padx=1,pady=1)
    result={"ok":False}

    # printer glyph
    ic = tk.Canvas(card,width=58,height=58,bg=P_ICONBG,highlightthickness=0)
    ic.place(x=(W-58)//2,y=26)
    ic.create_rectangle(14,13,44,24,outline=P_ACCENT,width=2)
    ic.create_rectangle(9,23,49,42,outline=P_ACCENT,width=2)
    ic.create_rectangle(15,37,43,51,outline=P_ACCENT,width=2)

    tk.Label(card,text="Printing",bg=P_CARD,fg=P_INK,font=("Segoe UI",23,"bold")).place(x=0,y=94,width=W)
    tk.Label(card,text=HEBREW_PRINT,bg=P_CARD,fg=P_MUTED,font=("Segoe UI",14)).place(x=0,y=138,width=W)
    tk.Label(card,text="This print costs money - please pay at the desk.",bg=P_CARD,fg=P_MUTED,
             font=("Segoe UI",11)).place(x=0,y=166,width=W)
    if docname:
        tk.Label(card,text=docname[:54],bg=P_CARD,fg=P_DOC,font=("Segoe UI",10)).place(x=20,y=194,width=W-40)

    tk.Label(card,text=cost,bg=P_CARD,fg=P_GREEN,font=("Segoe UI",40,"bold")).place(x=0,y=228,width=W)

    chip = tk.Frame(card,bg=P_CHIP); chip.place(x=(W-150)//2,y=306,width=150,height=30)
    tk.Label(chip,text="%d page%s"%(sheets,'' if sheets==1 else 's'),bg=P_CHIP,fg=P_DOC,
             font=("Segoe UI",10)).place(relx=0.5,rely=0.5,anchor="center")
    tk.Label(card,text="%d x %s%.2f"%(sheets,cur,rate),bg=P_CARD,fg=P_FAINT,
             font=("Segoe UI",9)).place(x=0,y=344,width=W)

    def paid(): result["ok"]=True; dlg.destroy()
    def cancel(): dlg.destroy()
    b1 = tk.Button(card,text="Paid  -  Print",command=paid,bg=P_BTN,fg=P_BTNTXT,relief="flat",
                   font=("Segoe UI",15,"bold"),cursor="hand2",activebackground="#16a34a",bd=0)
    b1.place(x=40,y=384,width=248,height=58)
    b2 = tk.Button(card,text="Cancel",command=cancel,bg=P_CARD,fg=P_MUTED,relief="flat",
                   font=("Segoe UI",12),cursor="hand2",activebackground=P_CHIP,activeforeground=P_INK,bd=0)
    b2.place(x=300,y=384,width=100,height=58)
    b1.bind("<Enter>",lambda e:b1.config(bg="#16a34a")); b1.bind("<Leave>",lambda e:b1.config(bg=P_BTN))
    b2.bind("<Enter>",lambda e:b2.config(bg=P_CHIP)); b2.bind("<Leave>",lambda e:b2.config(bg=P_CARD))
    dlg.bind("<Escape>", lambda e: cancel())
    dlg.protocol("WM_DELETE_WINDOW", cancel)
    dlg.update()
    try: dlg.grab_set()
    except Exception: pass
    b1.focus_set()
    root.wait_window(dlg)
    return result["ok"]

# ---------------- app ----------------
class App:
    def __init__(self, root):
        self.root=root; self.cfg=load_settings(); self.doc=None; self.page=0
        self.events=queue.Queue(); self._photo=None
        root.title(APP); root.configure(bg=BG); root.geometry("980x640"); root.minsize(880,560)
        self._style(); self._build()
        self.root.after(60, self._pump)

    def _style(self):
        s=ttk.Style()
        try: s.theme_use("clam")
        except Exception: pass
        s.configure("TLabel",background=CARD,foreground=FG,font=("Segoe UI",10))
        s.configure("Muted.TLabel",background=CARD,foreground=MUTED,font=("Segoe UI",9))
        s.configure("Card.TFrame",background=CARD)
        s.configure("TButton",font=("Segoe UI",10))
        s.configure("Accent.TButton",font=("Segoe UI",12,"bold"))
        s.configure("TEntry",fieldbackground="#0b1220",foreground=FG)
        s.configure("TSpinbox",fieldbackground="#0b1220",foreground=FG,arrowcolor=FG)

    def _build(self):
        top=tk.Frame(self.root,bg=BG); top.pack(fill="x",padx=16,pady=(14,6))
        tk.Label(top,text="Brother Print",bg=BG,fg=FG,font=("Segoe UI",18,"bold")).pack(side="left")
        tk.Label(top,text="IPP / AirPrint  -  pay before printing",bg=BG,fg=MUTED,
                 font=("Segoe UI",10)).pack(side="left",padx=12)

        body=tk.Frame(self.root,bg=BG); body.pack(fill="both",expand=True,padx=16,pady=8)
        left=tk.Frame(body,bg=CARD,width=320); left.pack(side="left",fill="y"); left.pack_propagate(False)
        right=tk.Frame(body,bg=CARD); right.pack(side="left",fill="both",expand=True,padx=(12,0))

        # ---- left controls ----
        pad={"padx":16}
        ttk.Button(left,text="Open PDF / image...",command=self.open_file).pack(fill="x",pady=(16,6),**pad)
        self.file_var=tk.StringVar(value="No file loaded.")
        ttk.Label(left,textvariable=self.file_var,style="Muted.TLabel",wraplength=280).pack(fill="x",**pad)

        box=ttk.Labelframe(left,text=" Options ")
        box.pack(fill="x",pady=(14,0),**pad)
        def rowlabel(parent,txt):
            f=tk.Frame(parent,bg=CARD); f.pack(fill="x",padx=10,pady=5)
            tk.Label(f,text=txt,bg=CARD,fg=FG,font=("Segoe UI",10),width=10,anchor="w").pack(side="left")
            return f
        r=rowlabel(box,"Copies"); self.copies_var=tk.StringVar(value=str(self.cfg["copies"]))
        ttk.Spinbox(r,from_=1,to=99,width=8,textvariable=self.copies_var).pack(side="left")
        r=rowlabel(box,"Pages"); self.pages_var=tk.StringVar(value="")
        e=tk.Entry(r,textvariable=self.pages_var,width=12,bg="#0b1220",fg=FG,insertbackground=FG,relief="flat")
        e.pack(side="left"); tk.Label(r,text="(blank = all)",bg=CARD,fg=MUTED,font=("Segoe UI",8)).pack(side="left",padx=6)
        r=rowlabel(box,"Quality"); self.dpi_var=tk.StringVar(value=str(self.cfg["dpi"]))
        ttk.Combobox(r,textvariable=self.dpi_var,values=["150","200","300"],width=6,state="readonly").pack(side="left")
        tk.Label(r,text="dpi",bg=CARD,fg=MUTED,font=("Segoe UI",8)).pack(side="left",padx=4)
        r=rowlabel(box,"Per sheet"); self.nup_var=tk.StringVar(value=str(self.cfg.get("nup",1)))
        ttk.Combobox(r,textvariable=self.nup_var,values=["1","2","4","6","9"],width=6,state="readonly").pack(side="left")
        tk.Label(r,text="pages/side",bg=CARD,fg=MUTED,font=("Segoe UI",8)).pack(side="left",padx=4)
        self.duplex_var=tk.BooleanVar(value=bool(self.cfg.get("duplex",True)))
        df=tk.Frame(box,bg=CARD); df.pack(fill="x",padx=10,pady=(0,8))
        tk.Checkbutton(df,text="Double-sided  (recommended)",variable=self.duplex_var,bg=CARD,fg=GREEN,
                       selectcolor="#0b1220",activebackground=CARD,activeforeground=GREEN,
                       font=("Segoe UI",9,"bold"),command=self.on_opts_change,bd=0,highlightthickness=0).pack(side="left")
        for _v in (self.copies_var,self.nup_var,self.pages_var):
            _v.trace_add("write", lambda *a: self.on_opts_change())

        pr=ttk.Labelframe(left,text=" Printer (Brother) ")
        pr.pack(fill="x",pady=(14,0),**pad)
        r=rowlabel(pr,"IP"); self.host_var=tk.StringVar(value=self.cfg["host"])
        tk.Entry(r,textvariable=self.host_var,width=16,bg="#0b1220",fg=FG,insertbackground=FG,relief="flat").pack(side="left")
        ttk.Button(pr,text="Check printer",command=self.check).pack(fill="x",padx=10,pady=(2,10))

        self.price_var=tk.StringVar(value="")
        tk.Label(left,textvariable=self.price_var,bg=CARD,fg=GREEN,font=("Segoe UI",11,"bold")).pack(pady=(12,0),**pad)

        self.print_btn=tk.Button(left,text="Print",command=self.do_print,bg=ACCENT,fg="white",
                                 font=("Segoe UI",14,"bold"),relief="flat",height=2,cursor="hand2")
        self.print_btn.pack(fill="x",side="bottom",pady=16,**pad)

        # ---- right preview ----
        head=tk.Frame(right,bg=CARD); head.pack(fill="x",padx=10,pady=8)
        self.prev_btn=tk.Button(head,text="<",width=3,command=lambda:self.turn(-1),state="disabled",relief="flat",bg="#334155",fg=FG)
        self.prev_btn.pack(side="left")
        self.next_btn=tk.Button(head,text=">",width=3,command=lambda:self.turn(1),state="disabled",relief="flat",bg="#334155",fg=FG)
        self.next_btn.pack(side="left",padx=(4,10))
        self.page_var=tk.StringVar(value="No document")
        tk.Label(head,textvariable=self.page_var,bg=CARD,fg=FG,font=("Segoe UI",10)).pack(side="left")
        self.canvas=tk.Canvas(right,bg="#020617",highlightthickness=0); self.canvas.pack(fill="both",expand=True,padx=10,pady=(0,10))
        self.canvas.bind("<Configure>", lambda e: self.render_preview())

        self.status_var=tk.StringVar(value="Open a PDF or image to begin.")
        bar=tk.Frame(self.root,bg="#0b1220"); bar.pack(fill="x",side="bottom")
        tk.Label(bar,textvariable=self.status_var,bg="#0b1220",fg=MUTED,font=("Segoe UI",9),anchor="w").pack(side="left",fill="x",expand=True,padx=12,pady=5)
        self.progress=ttk.Progressbar(bar,length=220,mode="determinate"); self.progress.pack(side="right",padx=12,pady=6)

    # ---- doc ----
    def open_file(self):
        p=filedialog.askopenfilename(initialdir=self.cfg.get("last_dir") or None,
            filetypes=[("Printable","*.pdf *.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff"),("All","*.*")])
        if p: self.load_path(p)

    def load_path(self, p):
        try:
            if self.doc: self.doc.close()
            self.doc=raster.Document(p); self.page=0; self.cfg["last_dir"]=os.path.dirname(p)
            self.file_var.set(os.path.basename(p)+f"  ({self.doc.page_count} page(s))")
            self.print_btn.config(state="normal")
            self.prev_btn.config(state="normal" if self.doc.page_count>1 else "disabled")
            self.next_btn.config(state="normal" if self.doc.page_count>1 else "disabled")
            self.update_price(); self.render_preview()
            self.status_var.set("Loaded. Review, then Print.")
        except Exception as e:
            messagebox.showerror(APP,f"Could not open file:\n{e}")

    def turn(self,d):
        if not self.doc: return
        self.page += d; self.render_preview()   # render_preview clamps to the side count

    def on_opts_change(self,*a):
        self.update_price()
        if self.doc: self.render_preview()

    def collect(self):
        def i(v,d):
            try: return int(v)
            except Exception: return d
        return {"copies":max(1,min(99,i(self.copies_var.get(),1))),
                "dpi":i(self.dpi_var.get(),200),"paper":self.cfg["paper"],"landscape":False,
                "pages":self.pages_var.get().strip(),"host":self.host_var.get().strip() or DEFAULTS["host"],
                "port":self.cfg["port"],"nup":max(1,i(self.nup_var.get(),1)),
                "duplex":bool(self.duplex_var.get())}

    def sheet_math(self, opts, pagecount):
        """(printed sides, billable physical sheets) for the given options."""
        per=opts["nup"]; sides=(pagecount+per-1)//per if pagecount else 0
        physical=((sides+1)//2 if opts["duplex"] else sides)*opts["copies"]
        return sides, physical

    def parse_pages(self,spec,n):
        if not spec: return list(range(n))
        out=[]
        for part in spec.split(","):
            part=part.strip()
            if "-" in part:
                a,b=part.split("-"); out+=list(range(int(a)-1,int(b)))
            elif part: out.append(int(part)-1)
        return [p for p in out if 0<=p<n] or list(range(n))

    def update_price(self):
        if not self.doc: self.price_var.set(""); return
        try: opts=self.collect()
        except Exception: return
        pages=len(self.parse_pages(opts["pages"],self.doc.page_count))
        _sides,sheets=self.sheet_math(opts,pages)
        extra=("" if opts["nup"]==1 else ", %d/side"%opts["nup"])+(", 2-sided" if opts["duplex"] else "")
        self.price_var.set("Cost: %s%.2f  -  %d sheet%s  (%d pg%s)"%(
            self.cfg["currency"],sheets*self.cfg["rate"],sheets,'' if sheets==1 else 's',pages,extra))

    def render_preview(self):
        if not self.doc:
            self.canvas.delete("all"); return
        try:
            opts=self.collect()
            pages=self.parse_pages(opts["pages"],self.doc.page_count)
            per=opts["nup"]; total=max(1,(len(pages)+per-1)//per)
            self.page=max(0,min(total-1,self.page))
            side_pages=pages[self.page*per:(self.page+1)*per]
            gray=compose_one_side(self.doc, side_pages, per, 110, opts["paper"])
            cw=self.canvas.winfo_width() or 600; ch=self.canvas.winfo_height() or 500
            self.canvas.delete("all")
            if gray is not None:
                iw,ih=gray.size; sc=min((cw-30)/iw,(ch-30)/ih,1.0)
                disp=gray.resize((max(1,int(iw*sc)),max(1,int(ih*sc))),Image.LANCZOS)
                self._photo=ImageTk.PhotoImage(disp)
                self.canvas.create_rectangle((cw-disp.width)//2,(ch-disp.height)//2,
                    (cw+disp.width)//2,(ch+disp.height)//2,fill="#ffffff",outline="#475569")
                self.canvas.create_image(cw//2,ch//2,image=self._photo)
            self.prev_btn.config(state="normal" if total>1 else "disabled")
            self.next_btn.config(state="normal" if total>1 else "disabled")
            if opts["duplex"] and per*total>1:
                sheet=self.page//2+1; face="Front" if self.page%2==0 else "Back"
                self.page_var.set("Sheet %d %s   (side %d of %d)"%(sheet,face,self.page+1,total))
            else:
                self.page_var.set("Sheet %d of %d"%(self.page+1,total))
        except Exception:
            pass

    def check(self):
        opts=self.collect()
        self.status_var.set("Checking printer...")
        def work():
            ok=ipp_reachable(opts["host"],opts["port"])
            self.events.put(("chk",ok,opts["host"]))
        threading.Thread(target=work,daemon=True).start()

    # ---- print (pay first) ----
    def do_print(self):
        try:
            self._do_print()
        except Exception as e:
            self.print_btn.config(state="normal")
            messagebox.showerror(APP, "Print error:\n%s" % e)

    def _do_print(self):
        if not self.doc:
            messagebox.showinfo(APP, "Open a PDF or image first (top-left button)."); return
        opts=self.collect(); pages=self.parse_pages(opts["pages"],self.doc.page_count)
        nsides,sheets=self.sheet_math(opts,len(pages))
        # fast reachability check so we don't churn through retries when it's offline
        offline = not ipp_reachable(opts["host"], opts["port"], timeout=1.5)
        if offline:
            if not messagebox.askyesno(APP,
                    "The printer at %s is not responding.\n\nIt may be offline (the wired connection arrives Sunday).\n\nShow the pay screen anyway, to test the flow?" % opts["host"]):
                self.status_var.set("Printer not reachable at %s." % opts["host"]); return
        # PAY FIRST (cost is per physical sheet - N-up + double-sided lower it)
        if not pay_prompt(self.root,sheets,self.cfg["rate"],self.cfg["currency"],os.path.basename(self.doc.path)):
            self.status_var.set("Cancelled - not printed."); return
        if offline:
            self.status_var.set("Test mode: printer offline, nothing sent.")
            messagebox.showinfo(APP, "Test only - the printer is offline, so nothing was sent.\n\nThis exact flow will print once the printer is connected Sunday.")
            return
        self.print_btn.config(state="disabled")
        self.progress.config(value=0,maximum=100); self.status_var.set("Preparing pages...")
        self.persist()
        jobname=os.path.basename(self.doc.path)[:60]
        def work():
            try:
                sides_img=compose_sides(self.doc,pages,opts["nup"],opts["dpi"],opts["paper"])
                side_jpegs=[page_to_jpeg(s) for s in sides_img]
                def prog(done,total): self.events.put(("prog",done/max(1,total)*100.0,"Sending side %d of %d..."%(done,total)))
                ok=print_sides(opts["host"],opts["port"],side_jpegs,opts["duplex"],opts["copies"],jobname,progress=prog)
                self.events.put(("pdone",0 if ok else 1,sheets,opts))
            except Exception as e:
                self.events.put(("perr",f"{type(e).__name__}: {e}"))
        threading.Thread(target=work,daemon=True).start()

    def persist(self):
        try:
            o=self.collect()
            for k in ("copies","dpi","host","nup","duplex"): self.cfg[k]=o[k]
            save_settings(self.cfg)
        except Exception: pass

    def _pump(self):
        try:
            while True:
                ev=self.events.get_nowait(); k=ev[0]
                if k=="prog":
                    self.progress.config(value=ev[1])
                    if ev[2]: self.status_var.set(ev[2])
                elif k=="chk":
                    self.status_var.set(("Printer reachable at "+ev[2]) if ev[1] else ("NOT reachable at "+ev[2]))
                elif k=="pdone":
                    _,failed,total,opts=ev; self.progress.config(value=100); self.print_btn.config(state="normal")
                    if failed==0:
                        self.status_var.set(f"Printed {total} sheet(s) to {opts['host']}. Collect at the printer.")
                        messagebox.showinfo(APP,f"Sent {total} sheet(s) to the Brother.\nCollect your pages at the printer.")
                    else:
                        self.status_var.set(f"{total-failed}/{total} sheet(s) sent; {failed} failed.")
                        messagebox.showwarning(APP,f"{failed} of {total} page(s) failed to send.\nCheck the printer connection and try again.")
                elif k=="perr":
                    self.progress.config(value=0); self.print_btn.config(state="normal")
                    self.status_var.set("Print failed."); messagebox.showerror(APP,f"Could not print:\n{ev[1]}")
        except queue.Empty:
            pass
        self.root.after(60,self._pump)

def main():
    root=tk.Tk(); app=App(root)
    # a file path argument (e.g. from the kiosk capture printer) auto-loads it
    for a in sys.argv[1:]:
        if os.path.isfile(a):
            root.after(300, lambda p=a: app.load_path(p)); break
    root.mainloop()

if __name__=="__main__":
    main()
