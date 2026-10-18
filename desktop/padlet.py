import customtkinter as ctk
import threading
import time
import random
import json
from curl_cffi import requests
from playwright.sync_api import sync_playwright
from tkinter import messagebox

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class PadletSender(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Padlet Sender")
        self.geometry("650x780")
        self.resizable(False, False)
        
        self.running = False
        self.worker_thread = None
        
        # Interceptor state
        self.endpoint = ""
        self.hdrs = {}
        self.payload_tpl = {}
        self.ready = False
        
        self.build_ui()

    def build_ui(self):
        ctk.CTkLabel(self, text="PADLET SENDER", font=ctk.CTkFont(size=22, weight="bold")).pack(pady=(20, 15))
        
        f = ctk.CTkFrame(self)
        f.pack(pady=10, padx=20, fill="x")

        # URL Input
        ctk.CTkLabel(f, text="Target URL:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.in_url = ctk.CTkEntry(f, width=350, placeholder_text="https://padlet.com/...")
        self.in_url.grid(row=0, column=1, padx=10, pady=10, sticky="w")

        # Login / Capture
        self.btn_login = ctk.CTkButton(f, text="1. Open Browser & Login", fg_color="#0973dc", hover_color="#005bb5", command=self.run_capture)
        self.btn_login.grid(row=1, column=0, columnspan=2, padx=10, pady=5, sticky="we")

        self.lbl_status = ctk.CTkLabel(f, text="Status: Waiting", text_color="gray")
        self.lbl_status.grid(row=2, column=0, columnspan=2, pady=(0, 10))

        # Payload Inputs
        ctk.CTkLabel(f, text="Subject:").grid(row=3, column=0, padx=10, pady=5, sticky="w")
        self.in_subj = ctk.CTkEntry(f, width=350, placeholder_text="Title here...")
        self.in_subj.grid(row=3, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(f, text="Body:").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        self.in_body = ctk.CTkEntry(f, width=350, placeholder_text="Content here...")
        self.in_body.grid(row=4, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(f, text="Limit (0=inf):").grid(row=5, column=0, padx=10, pady=5, sticky="w")
        self.in_cnt = ctk.CTkEntry(f, width=100)
        self.in_cnt.insert(0, "50")
        self.in_cnt.grid(row=5, column=1, padx=10, pady=5, sticky="w")

        ctk.CTkLabel(f, text="Delay (sec):").grid(row=6, column=0, padx=10, pady=5, sticky="w")
        self.in_dly = ctk.CTkEntry(f, width=100)
        self.in_dly.insert(0, "0.3")
        self.in_dly.grid(row=6, column=1, padx=10, pady=5, sticky="w")

        # Action Buttons
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=15)
        
        self.btn_start = ctk.CTkButton(btns, text="2. START", fg_color="#00ba7c", hover_color="#009a66", width=140, height=38, state="disabled", command=self.start_loop)
        self.btn_start.grid(row=0, column=0, padx=10)
        
        self.btn_stop = ctk.CTkButton(btns, text="STOP", fg_color="#e60000", hover_color="#b30000", width=140, height=38, state="disabled", command=self.stop_loop)
        self.btn_stop.grid(row=0, column=1, padx=10)

        # Logger
        self.log_box = ctk.CTkTextbox(self, width=600, height=220, font=("Consolas", 12))
        self.log_box.pack(padx=20, pady=10)
        self.log_box.configure(state="disabled")

    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def run_capture(self):
        url = self.in_url.get().strip()
        if not url:
            return messagebox.showerror("Error", "URL required.")

        self.btn_login.configure(state="disabled", text="Waiting for action...")
        self.lbl_status.configure(text="Status: Please log in and post once.", text_color="#f59e0b")

        def interceptor():
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=False)
                    ctx = browser.new_context()
                    page = ctx.new_page()
                    
                    c_data = {}
                    
                    def on_res(res):
                        if "api/9/wishes" in res.url and res.request.method == "POST":
                            try:
                                c_data["endpoint"] = res.url
                                c_data["hdrs"] = res.request.headers
                                c_data["body"] = res.request.post_data
                            except: pass
                            
                    page.on("response", on_res)
                    page.goto(url)
                    
                    # block until post is made
                    while not c_data:
                        page.wait_for_timeout(500)
                        
                    browser.close()
                    
                    self.endpoint = c_data["endpoint"]
                    self.hdrs = c_data["hdrs"]
                    self.payload_tpl = json.loads(c_data["body"])
                    
                    # strip transport-level headers
                    for k in ["content-length", "host", "content-type"]:
                        self.hdrs.pop(k, None)
                    
                    self.ready = True
                    self.lbl_status.configure(text="Status: Captured. Ready to fire.", text_color="#00ba7c")
                    self.btn_start.configure(state="normal")
                    self.btn_login.configure(state="normal", text="1. Open Browser & Login")
                    self._log("[+] Network captured successfully.")
                    
            except Exception as e:
                self.lbl_status.configure(text="Status: Capture failed.", text_color="#e60000")
                self.btn_login.configure(state="normal", text="1. Open Browser & Login")
                self._log(f"[-] Error: {str(e)}")

        threading.Thread(target=interceptor, daemon=True).start()

    def send_payload(self, subj: str, body: str) -> tuple[int, str]:
        pl = self.payload_tpl.copy()
        
        pl["subject"] = subj
        pl["body"] = f"<p>{body}</p>"
        pl["cid"] = f"c_new{random.randint(10000, 99999)}"
        
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        pl["created_at"] = now
        pl["updated_at"] = now
        
        r = requests.post(self.endpoint, json=pl, headers=self.hdrs, impersonate="chrome120", timeout=10)
        return r.status_code, r.text

    def start_loop(self):
        if not self.ready: return
        subj = self.in_subj.get().strip()
        body = self.in_body.get().strip()
        
        if not subj:
            return messagebox.showerror("Error", "Subject required.")

        try:
            lim = int(self.in_cnt.get().strip())
            dly = float(self.in_dly.get().strip())
        except ValueError:
            return messagebox.showerror("Error", "Numeric values required.")

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.running = True
        
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        self.worker_thread = threading.Thread(target=self._worker, args=(subj, body, lim, dly), daemon=True)
        self.worker_thread.start()

    def _worker(self, subj: str, body: str, lim: int, dly: float):
        self._log(f"[*] Loop started. Limit={lim}, Delay={dly}s")
        sent, errs = 0, 0
        backoff = 0

        while self.running:
            if 0 < lim <= sent:
                break
                
            # interruptible sleep
            sleep_t = dly + random.uniform(0, 0.2) + backoff
            while sleep_t > 0 and self.running:
                time.sleep(0.1)
                sleep_t -= 0.1
                
            if not self.running: break

            status, msg = self.send_payload(subj, body)
            
            if status in (200, 201):
                sent += 1
                self._log(f"  [+] Sent #{sent}")
                backoff = 0
            elif status == 429:
                errs += 1
                backoff = min(backoff + 3.0, 30.0)
                self._log(f"  [-] 429 Rate limit. Backoff -> {backoff}s")
            else:
                errs += 1
                self._log(f"  [-] Err {status} | {msg[:100]}")
                time.sleep(1)

        if self.running:
            self._log(f"\n[+] Finished. OK: {sent}, ERR: {errs}")
        self.stop_loop()

    def stop_loop(self):
        if self.running:
            self.running = False
            self._log("\n[!] Halted by user.")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

if __name__ == "__main__":
    app = PadletSender()
    app.mainloop()