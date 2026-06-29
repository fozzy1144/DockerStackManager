import customtkinter as ctk
from datetime import datetime


class LogPanel(ctk.CTkFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(header, text="Output Log", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(header, text="Clear", width=60, height=24, command=self.clear).pack(side="right")

        self._text = ctk.CTkTextbox(self, state="disabled", font=("Consolas", 12), wrap="word")
        self._text.pack(fill="both", expand=True, padx=8, pady=6)

        self._text.tag_config("info", foreground="#a0d8ef")
        self._text.tag_config("success", foreground="#90ee90")
        self._text.tag_config("error", foreground="#ff7f7f")
        self._text.tag_config("warn", foreground="#ffd700")

    def log(self, message: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self._text.configure(state="normal")
        self._text.insert("end", f"[{ts}] {message}\n", level)
        self._text.configure(state="disabled")
        self._text.see("end")

    def clear(self):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")
