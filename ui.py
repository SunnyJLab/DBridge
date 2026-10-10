#!/usr/bin/env python3
"""DBridge — 관리 UI (표준 라이브러리 tkinter).

데이터는 전부 ProxyServer 공개 API에서 온다 (하드코딩된 표시값 없음).

실행: python app.py
"""

import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import config_store
from dbtools.errors import error_code
from transport import TOOLS, __version__, create_server

# Apple이 번들한 구형 system Tk의 배포 중단 경고다. 이 앱의 타깃은 Windows이고,
# macOS에서는 목업 미리보기만 지원하므로 Tk를 import하기 전에 권장 환경 변수를 설정한다.
if sys.platform == "darwin":
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

APP_TITLE = "DBridge"

# ------------------------------------------------------------- 디자인 토큰
#
# 팔레트 프리셋 — 실행 시 인자로 선택: python ui.py [slate|nord|github|mono|sun]
#   slate  : 딥 네이비 사이드바 + 라이트 블루 액센트 (기본)
#   nord   : Nord 팔레트 기반 전체 다크 모드
#   github : GitHub Primer 라이트 — 개발자에게 익숙한 담백함
#   mono   : Linear/Vercel풍 모노크롬 — 무채색 + 블랙 액센트
#   sun    : Sun Valley light(Win11 Fluent) + 다크 콘솔 — theme_sv/sv.tcl 반입 파일 로드

PALETTES = {
    "slate": {
        "sidebar": "#131C2B", "sidebar_hover": "#1C2940", "sidebar_active": "#24344F",
        "side_text": "#8DA2BE", "side_text_hi": "#FFFFFF", "side_ok": "#4ADE80",
        "bg": "#EEF2F7", "card": "#FFFFFF", "border": "#DDE4EE",
        "text": "#17222F", "muted": "#6B7A90",
        "field": "#F5F8FC", "stripe": "#F7FAFD",
        "accent": "#2F6FED", "accent_hover": "#2860D0", "accent_down": "#1F4FAF",
        "accent_fg": "#FFFFFF", "select": "#DBE8FF",
        "ok": "#178A46", "ok_bg": "#DDF5E5",
        "warn": "#B45309", "warn_bg": "#FDEFD2",
        "err": "#C22B2B", "err_bg": "#FBE1E1",
        "console": "#0C1421", "console_fg": "#C9D6E6",
    },
    "nord": {
        "sidebar": "#242933", "sidebar_hover": "#2E3440", "sidebar_active": "#3B4252",
        "side_text": "#8FA0B8", "side_text_hi": "#ECEFF4", "side_ok": "#A3BE8C",
        "bg": "#2E3440", "card": "#3B4252", "border": "#434C5E",
        "text": "#ECEFF4", "muted": "#94A3BC",
        "field": "#333A48", "stripe": "#404A5E",
        "accent": "#81A1C1", "accent_hover": "#93B1CD", "accent_down": "#6D8FB0",
        "accent_fg": "#2E3440", "select": "#4C566A",
        "ok": "#A3BE8C", "ok_bg": "#37413B",
        "warn": "#EBCB8B", "warn_bg": "#443F33",
        "err": "#BF616A", "err_bg": "#432F33",
        "console": "#232831", "console_fg": "#D8DEE9",
    },
    "github": {
        "sidebar": "#24292F", "sidebar_hover": "#2F363D", "sidebar_active": "#383F47",
        "side_text": "#8B949E", "side_text_hi": "#FFFFFF", "side_ok": "#3FB950",
        "bg": "#F6F8FA", "card": "#FFFFFF", "border": "#D0D7DE",
        "text": "#1F2328", "muted": "#656D76",
        "field": "#F6F8FA", "stripe": "#F0F3F6",
        "accent": "#0969DA", "accent_hover": "#0757B4", "accent_down": "#054A9B",
        "accent_fg": "#FFFFFF", "select": "#DDF4FF",
        "ok": "#1A7F37", "ok_bg": "#DAFBE1",
        "warn": "#9A6700", "warn_bg": "#FFF8C5",
        "err": "#CF222E", "err_bg": "#FFEBE9",
        "console": "#0D1117", "console_fg": "#C9D1D9",
    },
    "mono": {
        "sidebar": "#0A0A0A", "sidebar_hover": "#171717", "sidebar_active": "#222222",
        "side_text": "#8F8F8F", "side_text_hi": "#FFFFFF", "side_ok": "#4ADE80",
        "bg": "#FAFAFA", "card": "#FFFFFF", "border": "#E5E5E5",
        "text": "#171717", "muted": "#737373",
        "field": "#F5F5F5", "stripe": "#FAFAFA",
        "accent": "#171717", "accent_hover": "#2E2E2E", "accent_down": "#000000",
        "accent_fg": "#FFFFFF", "select": "#E8E8E8",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#A16207", "warn_bg": "#FEF9C3",
        "err": "#B91C1C", "err_bg": "#FEE2E2",
        "console": "#111111", "console_fg": "#D4D4D4",
    },
    # Win11 Fluent 라이트. ttk 위젯은 Sun Valley 테마가 그리고, 아래 색은 tk 위젯(사이드바/카드/콘솔)용.
    "sun": {
        "sidebar": "#202020", "sidebar_hover": "#2D2D2D", "sidebar_active": "#383838",
        "side_text": "#9D9D9D", "side_text_hi": "#FFFFFF", "side_ok": "#6CCB5F",
        # bg=Win11 mica, card=Card.field 스프라이트 내부색(#FAFAFA)과 일치 — 이질감 없이 밀착
        "bg": "#F3F3F3", "card": "#FAFAFA", "border": "#E7E7E7",
        "text": "#1B1B1B", "muted": "#5F5F5F",
        "field": "#FFFFFF", "stripe": "#F2F2F2",
        "accent": "#005FB8", "accent_hover": "#1970CA", "accent_down": "#00458A",
        "accent_fg": "#FFFFFF", "select": "#CCE4F7",
        "ok": "#0F7B0F", "ok_bg": "#DFF6DD",
        "warn": "#9D5D00", "warn_bg": "#FFF4CE",
        "err": "#C42B1C", "err_bg": "#FDE7E9",
        "console": "#0C1421", "console_fg": "#C9D6E6",
        "chip_bg": "#EDEDED", "chip_fg": "#5C5C5C",
        "ttk_theme": "sun-valley-light",
    },
}

P = dict(PALETTES["slate"])


def set_palette(name):
    """config.ui.palette 로 팔레트 교체. App/setup_styles 이전에 호출. 쟘못된 이름은 ValueError.

    동일 P dict를 clear+update해 이전 팔레트의 잔여 키(예: sun의 ttk_theme)가 남지 않게 한다.
    ttk 스타일은 기동 시 적용되므로 변경은 재시작이 필요하다.
    """
    key = str(name).lower()
    if key not in PALETTES:
        raise ValueError(f"알 수 없는 팔레트: {name} (사용 가능: {', '.join(PALETTES)})")
    P.clear()
    P.update(dict(PALETTES[key]))
    return key

FONTS = {}  # init_fonts()에서 채움


def init_fonts(root):
    """Windows: Segoe UI / Cascadia Mono·Consolas. 없으면 플랫폼 기본으로 폴백."""
    families = set(tkfont.families(root))

    def pick(candidates, fallback):
        return next((f for f in candidates if f in families), fallback)

    ui = pick(["Segoe UI", "Malgun Gothic", "SF Pro Text", "Helvetica"], "TkDefaultFont")
    mono = pick(["Cascadia Mono", "Consolas", "D2Coding", "Menlo"], "Courier")
    # Windows 표준 UI 크기(9pt)를 본문 기준으로 잡아 같은 창에 더 많은 내용을 보인다.
    FONTS.update(
        title=(ui, 13, "bold"),
        h1=(ui, 11, "bold"),
        h2=(ui, 10, "bold"),
        body=(ui, 9),
        small=(ui, 8),
        small_b=(ui, 8, "bold"),
        big=(ui, 13, "bold"),
        mono=(mono, 9),
        mono_s=(mono, 8),
    )


def load_sun_valley_theme(root):
    """theme_sv/sv.tcl 반입 파일을 source 한다 (pip 불필요, MIT 라이선스).

    sv.tcl은 <<ThemeChanged>> 마다 tk_setPalette로 모든 tk 위젯 색을 덮어쓴다
    (다크 콘솔·사이드바 파괴) → 루트 클래스 바인딩을 제거하고 ttk 기본색은
    setup_styles에서 직접 지정한다.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    sv_tcl = os.path.join(base, "theme_sv", "sv.tcl")
    if not os.path.exists(sv_tcl):
        raise FileNotFoundError(f"Sun Valley 테마 파일이 없습니다: {sv_tcl}")
    root.tk.call("source", sv_tcl)
    root.tk.call("bind", root.winfo_class(), "<<ThemeChanged>>", "")


def setup_styles(root):
    """clam 베이스 위에 플랫 스타일 재정의. sun 팔레트는 Sun Valley 테마를 로드한다."""
    s = ttk.Style(root)

    if P.get("ttk_theme"):
        if P["ttk_theme"] not in s.theme_names():
            load_sun_valley_theme(root)
        s.theme_use(P["ttk_theme"])
        # Sun Valley 위에는 최소 보정만 한다. Accent.TButton은 테마 내장 스타일 그대로.
        s.configure(".", background=P["bg"], foreground=P["text"], troughcolor=P["bg"],
                    focuscolor=P["select"], selectbackground=P["accent"],
                    selectforeground=P["accent_fg"], fieldbackground=P["card"],
                    font=FONTS["body"])
        s.configure("Ghost.TButton", font=FONTS["body"])
        s.configure("Danger.TButton", foreground=P["err"], font=FONTS["body"])
        s.configure("Card.Treeview", background=P["card"], fieldbackground=P["card"],
                    rowheight=26, font=FONTS["body"])
        s.configure("Card.Treeview.Heading", background=P["card"], foreground=P["muted"],
                    font=FONTS["small_b"])
        s.configure("Card.TCheckbutton", background=P["card"], foreground=P["muted"],
                    font=FONTS["small"])
        s.map("Card.Treeview",
              background=[("selected", "focus", P["select"]), ("selected", P["border"])],
              foreground=[("selected", "focus", P["text"]), ("selected", P["text"])])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"Console.{orient}.TScrollbar", background=P["muted"],
                        troughcolor=P["console"], borderwidth=0, arrowsize=0)
        return

    s.theme_use("clam")
    s.configure(".", background=P["card"], foreground=P["text"], font=FONTS["body"],
                borderwidth=0, focuscolor=P["card"])

    s.configure("Accent.TButton", background=P["accent"], foreground=P["accent_fg"],
                padding=(11, 5), borderwidth=0)
    s.map("Accent.TButton",
          background=[("pressed", P["accent_down"]), ("active", P["accent_hover"])])

    s.configure("Ghost.TButton", background=P["card"], foreground=P["text"],
                padding=(10, 4), borderwidth=1, relief="solid",
                bordercolor=P["border"], lightcolor=P["card"], darkcolor=P["card"])
    s.map("Ghost.TButton", background=[("pressed", P["select"]), ("active", P["field"])])

    s.configure("Danger.TButton", background=P["err_bg"], foreground=P["err"],
                padding=(10, 4), borderwidth=0)
    s.map("Danger.TButton", background=[("active", P["err_bg"])])

    s.configure("Card.Treeview", background=P["card"], fieldbackground=P["card"],
                rowheight=26, borderwidth=0, font=FONTS["body"])
    s.configure("Card.Treeview.Heading", background=P["card"], foreground=P["muted"],
                borderwidth=0, font=FONTS["small_b"], padding=(6, 5))
    s.map("Card.Treeview",
          background=[("selected", "focus", P["select"]), ("selected", P["border"])],
          foreground=[("selected", "focus", P["text"]), ("selected", P["text"])])
    s.map("Card.Treeview.Heading", background=[("active", P["card"])])

    s.configure("Card.TCheckbutton", background=P["card"], foreground=P["muted"],
                font=FONTS["small"])
    # clam 기본 체크 표시는 흰 칸 안의 X라 "꺼짐"으로 읽힌다 → 켜지면 액센트로 채운다.
    s.map("Card.TCheckbutton",
          background=[("active", P["card"])],
          indicatorbackground=[("selected", P["accent"]), ("!selected", P["field"])],
          indicatorforeground=[("selected", P["accent_fg"])])

    # 입력 필드 공통 스타일 — 플랫 1px 보더, 포커스 시 액센트 링. 모든 폼이 같은 얼굴.
    field_style = dict(fieldbackground=P["field"], foreground=P["text"],
                       bordercolor=P["border"], lightcolor=P["field"],
                       darkcolor=P["field"], insertcolor=P["text"])
    focus_ring = dict(bordercolor=[("focus", P["accent"])],
                      lightcolor=[("focus", P["accent"])],
                      darkcolor=[("focus", P["accent"])])
    s.configure("TEntry", padding=(8, 5), **field_style)
    s.map("TEntry", **focus_ring)
    s.configure("TCombobox", padding=(8, 4), arrowcolor=P["muted"], arrowsize=13,
                background=P["field"], **field_style)
    # readonly 콤보의 clam 기본 회색 필드는 "비활성"으로 읽힌다 → 편집 가능한 것과 같은 톤으로
    s.map("TCombobox", fieldbackground=[("readonly", P["field"])],
          foreground=[("readonly", P["text"])],
          background=[("active", P["select"])], **focus_ring)
    # 콤보 팝다운 목록은 ttk 스타일 밖의 tk Listbox다 — option DB로만 꾸밀 수 있다.
    ui_font = f"{{{FONTS['body'][0]}}} {FONTS['body'][1]}"
    for pattern, value in [
        ("*TCombobox*Listbox.background", P["card"]),
        ("*TCombobox*Listbox.foreground", P["text"]),
        ("*TCombobox*Listbox.selectBackground", P["select"]),
        ("*TCombobox*Listbox.selectForeground", P["text"]),
        ("*TCombobox*Listbox.borderWidth", 0),
        ("*TCombobox*Listbox.font", ui_font),
    ]:
        root.option_add(pattern, value)

    s.configure("Vertical.TScrollbar", background=P["border"], troughcolor=P["card"],
                borderwidth=0, arrowsize=0)
    s.map("Vertical.TScrollbar", background=[("active", P["muted"])])

    for orient in ("Vertical", "Horizontal"):
        s.configure(f"Console.{orient}.TScrollbar", background=P["muted"],
                    troughcolor=P["console"], borderwidth=0, arrowsize=0)
        s.map(f"Console.{orient}.TScrollbar", background=[("active", P["console_fg"])])
    s.configure("Horizontal.TScrollbar", background=P["border"], troughcolor=P["card"],
                borderwidth=0, arrowsize=0)
    s.map("Horizontal.TScrollbar", background=[("active", P["muted"])])


def enable_windows_dpi_awareness():
    """고해상도(HiDPI)에서 글자 번짐 방지. Windows 전용, 실패해도 무해."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ------------------------------------------------------------- 공용 위젯


def card(parent):
    """카드 컨테이너. sun: Sun Valley 둥근 카드 스프라이트, 그 외: 흰 배경 + 1px 보더."""
    if P.get("ttk_theme"):
        return ttk.Frame(parent, style="Card.TFrame")
    return tk.Frame(parent, bg=P["card"], highlightbackground=P["border"],
                    highlightthickness=1, bd=0)


def card_header(parent, title, right_widget_maker=None):
    head = tk.Frame(parent, bg=P["card"])
    head.pack(fill="x", padx=12, pady=(10, 4))
    tk.Label(head, text=title, bg=P["card"], fg=P["text"], font=FONTS["h2"]).pack(side="left")
    if right_widget_maker:
        right_widget_maker(head).pack(side="right")
    return head


def copy_button(parent, get_text, style="Ghost.TButton"):
    """복사 버튼 — 성공 시 1.4초간 '복사됨 ✓' 피드백 (팝업 없음)."""
    btn = ttk.Button(parent, text="복사", style=style)

    def do_copy():
        btn.clipboard_clear()
        btn.clipboard_append(get_text())
        btn.config(text="복사됨 ✓")
        btn.after(1400, lambda: btn.config(text="복사"))

    btn.config(command=do_copy)
    return btn


def code_block(parent, height, font=None):
    """다크 콘솔 스타일 코드 블록 (읽기 전용)."""
    box = tk.Text(parent, height=height, font=font or FONTS["mono"], bg=P["console"],
                  fg=P["console_fg"], bd=0, highlightthickness=0, wrap="none",
                  padx=10, pady=8, insertbackground=P["console_fg"])
    box.config(state="disabled")
    return box


def scrolled(parent, make, dark=False, xscroll=False, **pack_kwargs):
    """make(container)로 만든 위젯에 세로 스크롤바를 붙여 pack하고 위젯을 돌려준다.

    dark=True는 콘솔 위젯용 — 래퍼와 스크롤바를 콘솔 색에 맞춰 옆에 흰 띠가 생기지 않게 한다.
    xscroll=True는 wrap="none" 위젯용 — 없으면 긴 줄이 그냥 잘려 보이지 않는다.
    """
    box = tk.Frame(parent, bg=P["console"] if dark else P["card"])
    box.pack(**pack_kwargs)
    box.grid_rowconfigure(0, weight=1)
    box.grid_columnconfigure(0, weight=1)
    widget = make(box)
    widget.grid(row=0, column=0, sticky="nsew")
    prefix = "Console." if dark else ""
    ybar = ttk.Scrollbar(box, orient="vertical", style=f"{prefix}Vertical.TScrollbar",
                         command=widget.yview)
    ybar.grid(row=0, column=1, sticky="ns")
    widget.configure(yscrollcommand=ybar.set)
    if xscroll:
        xbar = ttk.Scrollbar(box, orient="horizontal", style=f"{prefix}Horizontal.TScrollbar",
                             command=widget.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        widget.configure(xscrollcommand=xbar.set)
    return widget


def set_text(text_widget, content):
    state = text_widget.cget("state")
    text_widget.config(state="normal")
    text_widget.delete("1.0", "end")
    text_widget.insert("1.0", content)
    text_widget.config(state=state)


FORM_LABEL_W = 10  # 폼 라벨 공통 폭(문자) — 페이지가 달라도 입력칸 시작점을 맞춘다


def force_english_input(widget):
    """포커스가 오면 IME를 영문 모드로 돌린다 (Windows 전용, 실패 무해).

    설정값이 전부 식별자·DSN이라 기본은 영문이 맞다. 완전히 막지는 않으므로
    한/영 키로 다시 한글을 켤 수 있다. macOS 미리보기에서는 아무 것도 안 한다.
    """
    if sys.platform != "win32":
        return

    def to_english(_event=None):
        try:
            import ctypes
            imm = ctypes.windll.imm32
            hwnd = widget.winfo_id()
            himc = imm.ImmGetContext(hwnd)
            if himc:
                imm.ImmSetConversionStatus(himc, 0, 0)  # IME_CMODE_ALPHANUMERIC
                imm.ImmReleaseContext(hwnd, himc)
        except Exception:
            pass

    widget.bind("<FocusIn>", to_english, add="+")


def form_row(parent, label):
    row = tk.Frame(parent, bg=P["card"])
    row.pack(fill="x", pady=4)
    tk.Label(row, text=label, width=FORM_LABEL_W, anchor="w", bg=P["card"], fg=P["muted"],
             font=FONTS["small"]).pack(side="left")
    return row


def form_entry(parent, label, variable, **options):
    entry = ttk.Entry(form_row(parent, label), textvariable=variable,
                      font=FONTS["body"], **options)
    entry.pack(side="left", fill="x", expand=True)
    force_english_input(entry)
    return entry


def form_combo(parent, label, variable, values, **options):
    combo = ttk.Combobox(form_row(parent, label), textvariable=variable,
                         values=list(values), **options)
    combo.pack(side="left", fill="x", expand=True)
    force_english_input(combo)
    return combo


class AutoComplete:
    """ttk.Entry 아래에 후보 목록을 띄우는 자동완성.

    provider(text)가 후보 리스트를 돌려준다. None은 '적재 중' — 준비되면
    refresh()를 다시 부르면 된다. 키 입력 이벤트에만 반응하므로 프로그램적
    var.set()으로는 절대 뜨지 않는다. ↑/↓로 이동, Enter로 선택, Esc로 닫기.
    """

    MAX_ROWS = 8
    LOADING = "(불러오는 중…)"
    _IGNORE_KEYS = ("Up", "Down", "Return", "KP_Enter", "Escape", "Tab", "Left", "Right",
                    "Home", "End", "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L")

    def __init__(self, entry, provider):
        self.entry = entry
        self.provider = provider
        self.win = None
        self.inner = None
        self.rows = []
        self.active = -1
        self.pickable = False
        entry.bind("<KeyRelease>", self._on_key, add="+")
        entry.bind("<FocusOut>", lambda e: entry.after(150, self.close), add="+")
        entry.bind("<Escape>", lambda e: self.close(), add="+")
        entry.bind("<Return>", self._on_return, add="+")
        entry.bind("<Down>", lambda e: self._move(1), add="+")
        entry.bind("<Up>", lambda e: self._move(-1), add="+")

    def _on_key(self, event):
        if event.keysym in self._IGNORE_KEYS:
            return
        self.refresh()

    def refresh(self):
        """현재 입력으로 후보를 다시 그린다. 후보가 없거나 입력과 같으면 닫는다."""
        text = self.entry.get().strip()
        items = self.provider(text)
        if items is None:
            self._show([self.LOADING], pickable=False)
            return
        items = list(items)[: self.MAX_ROWS]
        if not items or items == [text]:
            self.close()
            return
        self._show(items, pickable=True)

    def _show(self, items, pickable):
        # Listbox는 셀 안쪽 여백을 못 준다 — 행을 Label로 만들어 패딩·호버를 직접 그린다.
        if self.win is None:
            self.win = tk.Toplevel(self.entry)
            self.win.wm_overrideredirect(True)
            self.win.configure(bg=P["border"])  # 1px 테두리 역할
            self.inner = tk.Frame(self.win, bg=P["card"])
            self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        for child in self.inner.winfo_children():
            child.destroy()
        self.pickable = pickable
        self.rows = []
        self.active = 0 if pickable else -1
        for i, item in enumerate(items):
            row = tk.Label(self.inner, text=item, bg=P["card"],
                           fg=P["text"] if pickable else P["muted"],
                           font=FONTS["body"], anchor="w", padx=10, pady=5)
            row.pack(fill="x")
            if pickable:
                row.bind("<Motion>", lambda e, idx=i: self._set_active(idx))
                row.bind("<Button-1>", lambda e, idx=i: self._pick(idx))
            self.rows.append(row)
        self._paint_active()
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
        self.win.update_idletasks()
        self.win.geometry(f"{self.entry.winfo_width()}x{self.inner.winfo_reqheight() + 2}+{x}+{y}")
        self.win.deiconify()
        self.win.lift()

    def _set_active(self, idx):
        if idx != self.active:
            self.active = idx
            self._paint_active()

    def _paint_active(self):
        for i, row in enumerate(self.rows):
            row.config(bg=P["select"] if i == self.active else P["card"])

    def _move(self, delta):
        if not (self.win and self.pickable):
            return None
        self._set_active((self.active + delta) % len(self.rows))
        return "break"

    def _on_return(self, _event):
        if self.win and self.pickable:
            self._pick(self.active)
            return "break"
        return None

    def _pick(self, idx):
        if not (self.win and self.pickable and 0 <= idx < len(self.rows)):
            return
        value = self.rows[idx].cget("text")
        self.close()
        self.entry.delete(0, "end")
        self.entry.insert(0, value)

    def close(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None
            self.inner = None
            self.rows = []
            self.active = -1
            self.pickable = False


class Chips(tk.Frame):
    """세그먼트 필터 (로그 레벨 등)."""

    def __init__(self, master, options, on_change, bg):
        super().__init__(master, bg=bg)
        self.on_change = on_change
        self.value = options[0]
        self.labels = {}
        for opt in options:
            lb = tk.Label(self, text=opt, font=FONTS["small_b"], padx=8, pady=2, cursor="hand2")
            lb.pack(side="left", padx=(0, 4))
            lb.bind("<Button-1>", lambda e, o=opt: self.set(o))
            self.labels[opt] = lb
        self._paint()

    def set(self, value):
        self.value = value
        self._paint()
        self.on_change(value)

    def _paint(self):
        for opt, lb in self.labels.items():
            if opt == self.value:
                lb.config(bg=P["accent"], fg=P["accent_fg"])
            else:
                lb.config(bg=P.get("chip_bg", P["border"]),
                          fg=P.get("chip_fg", P["muted"]))


# ------------------------------------------------------------- 배선 함수 (Tk 밖)


def build_status_rows(snapshot, catalog=None):
    """Convert snapshot()['connections'] into Treeview (iid, values, tags).

    iid is the connection name and is stable across refreshes so a selected
    row survives tick re-renders.
    catalog is server.readiness()['catalog']; pending + no error is connecting.
    """
    rows = []
    connections = (snapshot or {}).get("connections") or []
    seen: dict[str, int] = {}
    for i, conn in enumerate(connections):
        if not isinstance(conn, dict):
            continue
        name = conn.get("name") or ""
        if not name:
            name = f"#{i}"
        if name in seen:
            seen[name] += 1
            iid = f"{name}#{seen[name]}"
        else:
            seen[name] = 0
            iid = name
        host = conn.get("dsn") or ""
        raw_state = conn.get("state")
        error = conn.get("error")
        latency_ms = conn.get("latency_ms")
        latency = f"{latency_ms}ms" if latency_ms is not None else "-"
        pool = f"{conn.get('pool_used', 0)}/{conn.get('pool_max', 0)}"
        if raw_state == "ok":
            state, mark = "ok", "● 정상"
        elif error:
            code = error_code(error)
            state, mark = "err", f"● 실패 ({code})" if code else "● 실패"
        elif catalog == "pending":
            state, mark = "pending", "● 연결 중…"
        else:
            state, mark = "err", "● 실패"
        tags = (state,) + (("stripe",) if i % 2 else ())
        rows.append((iid, (name, host, mark, latency, pool), tags))
    return rows


def apply_status_rows(tree, rows):
    """Apply build_status_rows onto a Treeview-like object, keeping selection.

    tree must implement get_children, exists, item, insert, delete,
    set_children, selection, selection_set, focus.
    """
    wanted = {iid for iid, _values, _tags in rows}
    existing = set(tree.get_children())
    selected = list(tree.selection())
    focused = tree.focus()
    for iid in existing - wanted:
        tree.delete(iid)
    for iid, values, tags in rows:
        if tree.exists(iid):
            tree.item(iid, values=values, tags=tags)
        else:
            tree.insert("", "end", iid=iid, values=values, tags=tags)
    order = [iid for iid, _values, _tags in rows]
    if order:
        tree.set_children("", *order)
    keep = [iid for iid in selected if iid in wanted]
    if keep:
        tree.selection_set(keep)
        if focused in wanted:
            tree.focus(focused)
        else:
            tree.focus(keep[0])
    return order, keep


def _param_flag(param):
    if param.get("required"):
        return "필수"
    if "default" in param:
        return f"기본 {param['default']}"
    return "선택"


def build_tool_catalog(tools):
    """Copy tool metadata into the UI catalog shape (desc / params tuples)."""
    catalog = []
    copied = json.loads(json.dumps(list(tools or [])))
    for item in copied:
        params = []
        for param in item.get("params") or []:
            if isinstance(param, (list, tuple)) and len(param) >= 3:
                params.append((param[0], param[1], param[2]))
                continue
            if not isinstance(param, dict):
                continue
            params.append((
                param.get("name", ""),
                param.get("type", "string"),
                _param_flag(param),
            ))
        catalog.append({
            "name": item.get("name", ""),
            "desc": item.get("desc") or item.get("description") or "",
            "params": params,
            "example": item.get("example") or {"name": item.get("name", ""), "arguments": {}},
            "external": bool(item.get("external") or item.get("group") or item.get("sql")),
        })
    return catalog


def plain_text(text):
    """마크다운 강조 기호를 지운다. tk.Label은 마크다운을 렌더하지 않아 기호가 그대로 보인다."""
    return (text or "").replace("**", "").replace("`", "")


def format_params(params):
    """파라미터를 컬럼 맞춰 정렬한다. 이름 길이가 제각각이라 두 칸 띄우기로는 어긋났다."""
    rows = [(str(n), f"({t})", str(d)) for n, t, d in params]
    if not rows:
        return "(없음)"
    name_w = max(len(n) for n, _t, _d in rows)
    type_w = max(len(t) for _n, t, _d in rows)
    return "\n".join(f"{n:<{name_w}}  {t:<{type_w}}  {d}" for n, t, d in rows)


def parse_tool_arguments(raw):
    """Parse the editor JSON into an arguments dict; ValueError on failure.

    Blank input means no arguments ({}). A non-object top level is rejected.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 형식 오류: {exc.msg} (줄 {exc.lineno})") from None
    if not isinstance(value, dict):
        raise ValueError("인자는 JSON 객체({...})여야 합니다")
    return value


def load_tool_catalog(server):
    """Call server.tools() and convert. Empty success vs fetch failure.

    Returns (catalog, error_message). error_message is None on success,
    including an approved empty list. Exceptions are logged and not raised.
    """
    try:
        live = server.tools()
    except Exception:
        logging.getLogger("ui").exception("도구 목록 조회 실패")
        return [], "도구 목록 조회 실패"
    if live is None:
        live = []
    return build_tool_catalog(live), None


class ServerLifecycle:
    """Serialize stop/start/reload; request order is UI-thread seq, not lock order."""

    def __init__(self, server, create_server, *, lock=None):
        self._lock = threading.Lock() if lock is None else lock
        self._seq_lock = threading.Lock()
        self._server = server
        self._create = create_server
        self._seq = 0
        self._latest_applied_seq = 0
        self._closing = False

    def current(self):
        return self._server

    def next_seq(self):
        """Assign a monotonic request seq on the UI thread before spawning a worker."""
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def reload(self, config, seq=None):
        if seq is None:
            seq = self.next_seq()
        snapshot = json.loads(json.dumps(config))
        with self._lock:
            if self._closing or seq < self._latest_applied_seq:
                return self._server
            old = self._server
            if old is not None:
                old.stop()
            if self._closing or seq < self._latest_applied_seq:
                return self._server
            new = self._create(snapshot)
            try:
                new.start()
            except Exception:
                try:
                    new.stop()
                except Exception:
                    logging.getLogger("proxy").exception("failed start leftover stop failed")
                raise
            if self._closing or seq < self._latest_applied_seq:
                try:
                    new.stop()
                except Exception:
                    logging.getLogger("proxy").exception("superseded server stop failed")
                return self._server
            self._server = new
            self._latest_applied_seq = seq
            return new

    def stop(self, seq=None):
        if seq is None:
            seq = self.next_seq()
        with self._lock:
            if self._closing or seq < self._latest_applied_seq:
                return self._server
            current = self._server
            if current is not None:
                current.stop()
            self._latest_applied_seq = seq
            return current

    def start(self, config, seq=None):
        if seq is None:
            seq = self.next_seq()
        snapshot = json.loads(json.dumps(config))
        with self._lock:
            if self._closing or seq < self._latest_applied_seq:
                return self._server
            current = self._server
            listening = False
            if current is not None:
                try:
                    listening = bool(current.snapshot().get("listening"))
                except Exception:
                    listening = False
            if listening:
                self._latest_applied_seq = seq
                return current
            if current is not None:
                current.stop()
            if self._closing or seq < self._latest_applied_seq:
                return self._server
            new = self._create(snapshot)
            try:
                new.start()
            except Exception:
                try:
                    new.stop()
                except Exception:
                    logging.getLogger("proxy").exception("failed start leftover stop failed")
                raise
            if self._closing or seq < self._latest_applied_seq:
                try:
                    new.stop()
                except Exception:
                    logging.getLogger("proxy").exception("superseded server stop failed")
                return self._server
            self._server = new
            self._latest_applied_seq = seq
            return new

    def close(self):
        with self._lock:
            self._closing = True
            current = self._server
            if current is not None:
                current.stop()
            return current


def mcp_url(config):
    proxy = (config or {}).get("proxy") or {}
    host = proxy.get("host", "127.0.0.1")
    port = proxy.get("port", 8811)
    return f"http://{host}:{port}/mcp"


def claude_cli_snippet(config):
    return f"claude mcp add --transport http dbridge {mcp_url(config)}"


def claude_json_snippet(config):
    return json.dumps(
        {"mcpServers": {"dbridge": {"type": "http", "url": mcp_url(config)}}},
        indent=2,
        ensure_ascii=False,
    )


def format_uptime(started_at, now=None):
    if not started_at:
        return "00:00:00"
    now = time.time() if now is None else now
    up = max(0, int(now - started_at))
    return f"{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}"


def status_listening_label(snapshot, readiness=None):
    listening = bool((snapshot or {}).get("listening"))
    lifecycle = (readiness or {}).get("lifecycle")
    if listening:
        catalog = (readiness or {}).get("catalog")
        if catalog == "error":
            return "DB 연결 실패", "warn", "● DB 연결 실패"
        if catalog == "pending":
            return "연결 중…", "warn", "● 연결 중…"
        return "실행 중", "ok", "● 실행 중"
    if lifecycle == "stopping":
        return "중지 중", "warn", "● 중지 중"
    return "중지됨", "err", "● 중지됨"


# ------------------------------------------------------------- 상태 페이지


class StatusPage(tk.Frame):
    """실제 연동: core/status.py 스냅샷 dict를 after() 폴링으로 읽는다."""

    def __init__(self, master, app):
        super().__init__(master, bg=P["bg"])
        self.app = app
        self._busy = False
        self.stat_vars = {}

        # 상단 스탯 카드 3개 — 서버 상태는 헤더 pill이 이미 보여줘 카드로 두지 않는다.
        # 균등 고정 폭이라 값 텍스트(가동 시간 등)가 바뀌어도 흔들리지 않는다.
        stats = tk.Frame(self, bg=P["bg"])
        stats.pack(fill="x")
        stats.grid_columnconfigure((0, 1, 2), weight=1, uniform="stat")
        # 첫 refresh() 전까지 보여줄 값. 목업 더미가 아니라 설정·중립값에서 온다.
        for i, (key, label, init) in enumerate([
            ("addr", "MCP 주소", mcp_url(app.config_data).removeprefix("http://")),
            ("uptime", "가동 시간", "00:00:00"),
            ("requests", "처리 요청", "0건"),
        ]):
            c = card(stats)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            tk.Label(c, text=label, bg=P["card"], fg=P["muted"], font=FONTS["small"]).pack(
                anchor="w", padx=12, pady=(8, 0))
            var = tk.StringVar(value=init)
            tk.Label(c, textvariable=var, bg=P["card"], fg=P["text"], font=FONTS["big"]).pack(
                anchor="w", padx=12, pady=(1, 8))
            self.stat_vars[key] = var

        # 커넥션 테이블 카드
        conn_card = card(self)
        conn_card.pack(fill="both", expand=True, pady=(10, 0))
        card_header(conn_card, "DB 커넥션",
                    lambda head: ttk.Button(head, text="새로고침", style="Ghost.TButton",
                                            command=self.refresh))
        # 상태는 짧게만 (정상 / 연결 중 / 실패+코드). 상세는 로그 페이지.
        # 지연·풀은 숫자라 오른쪽 정렬하고 폭을 고정해, 남는 폭은 앞 세 컬럼이 가져간다.
        cols = ("name", "host", "state", "latency", "pool")
        heads = ("이름", "호스트", "상태", "지연", "풀")
        widths = (120, 190, 220, 60, 50)
        aligns = ("w", "w", "w", "e", "center")
        self.tree = scrolled(
            conn_card,
            lambda box: ttk.Treeview(box, columns=cols, show="headings",
                                     style="Card.Treeview", height=5),
            fill="both", expand=True, padx=12, pady=(0, 6))
        for c_, h, w, a in zip(cols, heads, widths, aligns):
            # 머리글 기본 정렬은 가운데라 왼쪽 정렬된 데이터와 어긋나 보였다.
            self.tree.heading(c_, text=h, anchor=a)
            self.tree.column(c_, width=w, minwidth=w, anchor=a,
                             stretch=(a == "w"))
        self.tree.tag_configure("stripe", background=P["stripe"])
        # 정상 행 전체를 초록으로 칠하면 표가 시끄럽다 — 오류만 색으로 띄운다.
        self.tree.tag_configure("ok", foreground=P["text"])
        self.tree.tag_configure("pending", foreground=P["warn"])
        self.tree.tag_configure("err", foreground=P["err"])
        self.winfo_toplevel().bind("<Button-1>", self._blur_tree_if_outside, add="+")

        foot = tk.Frame(conn_card, bg=P["card"])
        foot.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(foot, text="커넥션 테스트", style="Accent.TButton",
                   command=self._test_selected).pack(side="left")
        self.stop_btn = ttk.Button(foot, text="서버 중지", style="Danger.TButton",
                                   command=self._toggle_server)
        self.stop_btn.pack(side="left", padx=8)
        self.test_var = tk.StringVar()
        self.test_label = tk.Label(foot, textvariable=self.test_var, bg=P["card"], fg=P["muted"],
                                   font=FONTS["small_b"])
        self.test_label.pack(side="left", padx=8)

        self._status_clear_id = None

        self.refresh()
        self._tick()

    def refresh(self):
        snap = self.app.server.snapshot()
        self._render_snapshot(snap)

    def _render_snapshot(self, snap):
        readiness = None
        try:
            readiness = self.app.server.readiness()
        except Exception:
            readiness = None
        catalog = (readiness or {}).get("catalog")
        apply_status_rows(self.tree, build_status_rows(snap, catalog))
        _state_text, kind, pill_text = status_listening_label(snap, readiness)
        # 중지된 서버는 과거 started_at이 남아있어도 가동 시간이 아니다 — 0으로 고정하고,
        # 재시작하면 새 started_at이 다시 매겨지므로 그때부터 다시 증가한다.
        started_at = snap.get("started_at") if snap.get("listening") else None
        self.stat_vars["uptime"].set(format_uptime(started_at))
        self.stat_vars["requests"].set(f"{int(snap.get('request_count') or 0):,}건")
        self.stat_vars["addr"].set(f"{snap.get('host')}:{snap.get('port')}/mcp")
        self.app.set_running(pill_text, kind)
        listening = bool(snap.get("listening"))
        self.stop_btn.config(text="서버 중지" if listening else "서버 시작")

    def _blur_tree_if_outside(self, event):
        """트리 밖 클릭이면 위젯 포커스만 뺀다. selection은 커넥션 테스트용으로 남긴다."""
        if self.focus_get() is not self.tree:
            return
        widget = event.widget
        box = getattr(self.tree, "master", None)
        inside = widget is self.tree
        if not inside and box is not None:
            try:
                inside = str(widget).startswith(str(box))
            except (tk.TclError, AttributeError):
                inside = False
        if inside:
            return
        self.focus_set()

    def _set_status(self, text, kind="muted", clear_after=None):
        after_id = getattr(self, "_status_clear_id", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            self._status_clear_id = None
        self.test_var.set(text)
        self.test_label.config(fg=P.get(kind, P["muted"]))
        if clear_after is not None:
            self._status_clear_id = self.after(
                clear_after, lambda: self._set_status("", "muted"))

    def _test_selected(self):
        sel = self.tree.selection()
        if not sel:
            self._set_status("커넥션을 먼저 선택하세요", "muted", clear_after=1500)
            return
        name = self.tree.item(sel[0], "values")[0]
        self._set_status(f"{name}: 테스트 중…", "warn")

        def work():
            try:
                result = self.app.server.test_connection(name)
            except Exception as exc:
                result = {"ok": False, "latency_ms": None, "message": str(exc)}
            self.app.enqueue_ui_done(self._on_test_done, name, result)

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, name, result):
        if result.get("ok"):
            latency = result.get("latency_ms")
            suffix = f" ({latency}ms)" if latency is not None else ""
            self._set_status(f"{name}: 연결 정상{suffix}", "ok", clear_after=2500)
        else:
            message = result.get("message") or "실패"
            self._set_status(f"{name}: ✘ {message}", "err", clear_after=8000)

    def _toggle_server(self):
        if self._busy:
            return
        self._busy = True
        self.stop_btn.config(state="disabled")
        listening = bool(self.app.server.snapshot().get("listening"))
        seq = self.app.lifecycle.next_seq()

        def work():
            ok, message = True, ""
            try:
                if listening:
                    self.app.lifecycle.stop(seq=seq)
                else:
                    self.app.lifecycle.start(self.app.config_data, seq=seq)
            except Exception as extra:
                logging.getLogger("proxy").exception("서버 시작/중지 실패")
                ok, message = False, str(extra)
            self.app.enqueue_ui_done(self._on_toggle_done, ok, message)

        threading.Thread(target=work, daemon=True).start()

    def _on_toggle_done(self, ok, message):
        self._busy = False
        self.stop_btn.config(state="normal")
        if not ok:
            self._set_status(f"✘ {message}", "err", clear_after=8000)
        self.refresh()

    def _tick(self):
        try:
            self._render_snapshot(self.app.server.snapshot())
        finally:
            self.after(1000, self._tick)


# ------------------------------------------------------- Claude 연동 페이지


class ClaudePage(tk.Frame):
    """실제 연동: config의 proxy.host/port로 HTTP MCP URL 스니펫을 동적 생성."""

    def __init__(self, master, app):
        super().__init__(master, bg=P["bg"])
        self.app = app
        self.cli_box = self._section("CLI 등록", 1)
        self.json_box = self._section(".mcp.json (프로젝트)", 9)
        tk.Label(self, text="앱 실행 중에만 연결됩니다 · 주소는 설정의 proxy.host/port를 따릅니다",
                 bg=P["bg"], fg=P["muted"], font=FONTS["small"]).pack(anchor="w", pady=(8, 0))
        self.refresh()

    def _section(self, title, height):
        c = card(self)
        c.pack(fill="x", pady=(0, 10))
        box = code_block(c, height)
        card_header(c, title, lambda head: copy_button(head, lambda b=box: b.get("1.0", "end-1c")))
        box.pack(fill="x", padx=12, pady=(0, 12))
        return box

    def refresh(self):
        set_text(self.cli_box, claude_cli_snippet(self.app.config_data))
        set_text(self.json_box, claude_json_snippet(self.app.config_data))


# ------------------------------------------------------------- 도구 페이지


class ToolsPage(tk.Frame):
    """실제 연동: core/registry.py 의 tool 메타데이터를 그대로 표시."""

    def __init__(self, master, app):
        super().__init__(master, bg=P["bg"])
        self.app = app
        self.tools = []
        left = card(self)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=18, bd=0, highlightthickness=0,
                                  bg=P["card"], fg=P["text"], font=FONTS["body"],
                                  selectbackground=P["select"], selectforeground=P["text"],
                                  activestyle="none", exportselection=False)
        self.listbox.pack(fill="y", expand=True, padx=8, pady=10)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        right = card(self)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        body = tk.Frame(right, bg=P["card"])
        body.pack(fill="both", expand=True, padx=12, pady=10)

        self.name_var = tk.StringVar()
        tk.Label(body, textvariable=self.name_var, bg=P["card"], fg=P["text"],
                 font=FONTS["h1"]).pack(anchor="w")

        # 설명(좌)과 파라미터(우)를 같은 행에 나란히 둔다 — 설명은 스크롤
        # 가능한 고정 높이만 차지하고, 아래 인자/결과가 카드 전체 폭과 남는 세로
        # 폭을 다 가져간다.
        top = ttk.Panedwindow(body, orient="horizontal")
        top.pack(fill="x", pady=(2, 8))

        desc_pane = tk.Frame(top, bg=P["card"])
        top.add(desc_pane, weight=1)
        params_pane = tk.Frame(top, bg=P["card"])
        top.add(params_pane, weight=1)
        # weight는 창 크기 변경 시 비율만 잡을 뿐 초기 샤시 위치는 직접 잡아야 한다.
        # 생성 직후엔 창이 아직 매핑되지 않아 winfo_width가 1이다 — 첫 <Configure>를 기다린다.
        self._top_paned = top
        self._sash_set = False
        top.bind("<Configure>", self._init_sash)

        tk.Label(desc_pane, text="설명", bg=P["card"], fg=P["text"],
                 font=FONTS["h2"]).pack(anchor="w")
        # 설명은 길 수 있으니 고정 높이 스크롤 박스로 접어 파라미터와 높이를 맞춘다.
        self.desc = scrolled(
            desc_pane,
            lambda box: tk.Text(box, height=5, font=FONTS["body"], bg=P["card"],
                                fg=P["muted"], bd=0, highlightthickness=0, wrap="word",
                                padx=0, pady=0),
            fill="both", expand=True, pady=(4, 0))
        self.desc.config(state="disabled")

        tk.Label(params_pane, text="파라미터", bg=P["card"], fg=P["text"],
                 font=FONTS["h2"]).pack(anchor="w")
        self.params = scrolled(
            params_pane,
            lambda box: tk.Text(box, height=5, font=FONTS["mono_s"], bg=P["field"],
                                fg=P["text"], bd=0, highlightthickness=0, padx=8, pady=6),
            fill="both", expand=True, pady=(4, 0))
        self.params.config(state="disabled")

        # 인자/결과는 세로 분할 패널 — 인자는 기본 높이만, 결과가 남는 공간을 다
        # 가져가고 필요하면 샤시를 끌어 비율을 바꾼다.
        io_pane = ttk.Panedwindow(body, orient="vertical")
        io_pane.pack(fill="both", expand=True)

        args_pane = tk.Frame(io_pane, bg=P["card"])
        io_pane.add(args_pane, weight=0)
        head = tk.Frame(args_pane, bg=P["card"])
        head.pack(fill="x")
        tk.Label(head, text="인자 (JSON)", bg=P["card"], fg=P["text"],
                 font=FONTS["h2"]).pack(side="left")
        self.run_button = ttk.Button(head, text="실행", style="Accent.TButton",
                                     command=self._run_tool)
        self.run_button.pack(side="right")
        ttk.Button(head, text="예제", style="Ghost.TButton",
                   command=self._reset_args).pack(side="right", padx=(0, 8))
        self.args = scrolled(
            args_pane,
            lambda box: tk.Text(box, height=6, font=FONTS["mono_s"], bg=P["console"],
                                fg=P["console_fg"], bd=0, highlightthickness=0, wrap="none",
                                padx=10, pady=8, insertbackground=P["console_fg"]),
            dark=True, xscroll=True, fill="both", expand=True, pady=(4, 8))
        force_english_input(self.args)

        result_pane = tk.Frame(io_pane, bg=P["card"])
        io_pane.add(result_pane, weight=1)
        rhead = tk.Frame(result_pane, bg=P["card"])
        rhead.pack(fill="x")
        tk.Label(rhead, text="결과", bg=P["card"], fg=P["text"],
                 font=FONTS["h2"]).pack(side="left")
        self.result_info = tk.StringVar()
        tk.Label(rhead, textvariable=self.result_info, bg=P["card"], fg=P["muted"],
                 font=FONTS["small"]).pack(side="left", padx=8)
        copy_button(rhead, lambda: self.result.get("1.0", "end-1c")).pack(side="right")
        self.result = scrolled(result_pane, lambda box: code_block(box, 10, FONTS["mono_s"]),
                               dark=True, xscroll=True, fill="both", expand=True, pady=(4, 0))

        self.refresh()

    def _example_arguments(self):
        example = self._current().get("example") or {}
        args = example.get("arguments") if isinstance(example, dict) else None
        return args if isinstance(args, dict) else {}

    def _reset_args(self):
        set_text(self.args, json.dumps(self._example_arguments(), indent=2, ensure_ascii=False))

    def _run_tool(self):
        tool = self._current()
        name = tool["name"]
        try:
            arguments = parse_tool_arguments(self.args.get("1.0", "end"))
        except ValueError as exc:
            set_text(self.result, f"✘ {exc}")
            return
        server = self.app.server
        if server is None:
            set_text(self.result, "✘ 서버가 실행 중이 아닙니다 · [상태]에서 서버를 시작하세요")
            return
        set_text(self.result, f"{name} 실행 중…")
        self.result_info.set("")
        self.run_button.config(state="disabled")

        def work():
            started = time.monotonic()
            try:
                payload = server.call_tool(name, arguments)
                text = json.dumps(payload, indent=2, ensure_ascii=False)
            except Exception as exc:
                text = f"✘ 실행 실패: {exc}"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self.app.enqueue_ui_done(self._on_run_done, text, elapsed_ms)

        threading.Thread(target=work, daemon=True).start()

    def _on_run_done(self, text, elapsed_ms=None):
        set_text(self.result, text)
        if elapsed_ms is not None:
            self.result_info.set(f"{elapsed_ms:,}ms")
        self.run_button.config(state="normal")

    def _current(self):
        idx = self.listbox.curselection()
        return self.tools[idx[0] if idx else 0]

    def refresh(self):
        self.tools, error = self.app.tool_catalog()
        self.listbox.delete(0, "end")
        if error:
            self.name_var.set("조회 실패")
            set_text(self.desc, error)
            set_text(self.params, "(없음)")
            set_text(self.args, "")
            set_text(self.result, "")
            self.run_button.config(state="disabled")
            return
        for tool in self.tools:
            prefix = "◆ " if tool.get("external") else "  "
            self.listbox.insert("end", prefix + tool["name"])
        if self.tools:
            self.listbox.selection_set(0)
            self._on_select()
        else:
            self.name_var.set("(없음)")
            set_text(self.desc, "등록된 도구가 없습니다.")
            set_text(self.params, "(없음)")
            set_text(self.args, "")
            set_text(self.result, "")
            self.run_button.config(state="disabled")

    def _init_sash(self, event):
        """패널이 실제 폭을 가진 첫 <Configure>에서 따 1회 초기 샤시 위치를 잡는다.

        설명/파라미터가 동등하게 50:50으로 시작하도록 중간에 샤을 놓는다.
        사용자가 수동으로 끌어 조정한 후 폭이 다시 변하는 이후 <Configure>에서는
        되돌리지 않도록 1회만 적용한다.
        """
        if self._sash_set or event.width <= 1:
            return
        self._sash_set = True
        self._top_paned.sashpos(0, max(1, int(event.width * 0.5)))

    def _on_select(self, _event=None):
        if not self.tools:
            return
        tool = self._current()
        self.name_var.set(tool["name"])
        set_text(self.desc, plain_text(tool["desc"]))
        params = format_params(tool["params"])
        set_text(self.params, params)
        self._reset_args()
        set_text(self.result, "")
        self.result_info.set("")
        self.run_button.config(state="normal")


# ------------------------------------------------------------- 공통코드 설정 페이지


class ExtensionPage(tk.Frame):
    """실제 연동: config.json 최상위 common_code(테이블명 하나)를 저장·확인·해제한다.

    등록의 목적은 "어느 테이블인가"를 확정하는 것뿐이다. 컬럼 역할은 저장하지
    않는다 — 에이전트가 대상 컬럼명을 코드 그룹키로 써서 이 테이블을 조회한다.
    테이블명·컬럼명은 어디에도 하드코딩하지 않는다.
    """

    def __init__(self, master, app):
        super().__init__(master, bg=P["bg"])
        self.app = app
        self.cc_connection = tk.StringVar()
        self.cc_table = tk.StringVar()
        self._table_cache = {}      # 커넥션명 → ["OWNER.TABLE", ...]
        self._tables_loading = set()  # 비동기 적재 중인 커넥션명
        self._build(self)
        self.refresh_connections()

    def _build(self, parent):
        # 현재 등록 카드 — 지금 무엇이 등록돼 있는지부터 보여준다.
        current = card(parent)
        current.pack(fill="x")
        card_header(current, "현재 등록",
                    lambda head: ttk.Button(head, text="등록 해제", style="Danger.TButton",
                                            command=self._remove))
        self.cc_current = tk.StringVar()
        self.cc_current_label = tk.Label(current, textvariable=self.cc_current,
                                         bg=P["card"], fg=P["text"], font=FONTS["mono"],
                                         anchor="w")
        self.cc_current_label.pack(fill="x", padx=12)
        tk.Label(current, text="에이전트가 코드값의 의미를 해석할 때 참조하는 공통코드 테이블입니다",
                 bg=P["card"], fg=P["muted"], font=FONTS["small"], anchor="w").pack(
            fill="x", padx=12, pady=(2, 10))

        # 등록·변경 카드 — 폼 입력 후 저장, 조회 확인으로 실제 접근까지 검증.
        pane = card(parent)
        pane.pack(fill="x", pady=(10, 0))
        card_header(pane, "등록 · 변경")
        form = tk.Frame(pane, bg=P["card"])
        form.pack(fill="x", padx=12, pady=(0, 4))
        self.cc_conn_combo = form_combo(form, "커넥션", self.cc_connection, (),
                                        state="readonly")
        self.cc_table_entry = form_entry(form, "테이블", self.cc_table)
        # 타이핑하면 캐시된 테이블 목록에서 걸러 아래에 후보를 띄운다.
        self.cc_autocomplete = AutoComplete(self.cc_table_entry, self._table_candidates)

        foot = tk.Frame(pane, bg=P["card"])
        foot.pack(fill="x", padx=12, pady=(8, 10))
        ttk.Button(foot, text="저장", style="Accent.TButton",
                   command=self._save).pack(side="left")
        ttk.Button(foot, text="조회", style="Ghost.TButton",
                   command=self._verify).pack(side="left", padx=6)
        self.cc_status = tk.StringVar()
        self.cc_status_label = tk.Label(
            foot, textvariable=self.cc_status, bg=P["card"], fg=P["muted"],
            font=FONTS["small"], anchor="e", justify="right")
        self.cc_status_label.pack(side="right", fill="x", expand=True, padx=(12, 0))

    def _set_status(self, text, kind="muted"):
        self.cc_status.set(text)
        self.cc_status_label.config(fg=P.get(kind, P["muted"]))

    def _connection_names(self):
        return [item["name"] for item in self.app.config_data["connections"]]

    def _call(self, tool, **arguments):
        """도구 1회 호출. 서버가 없거나 오류면 (None, 메시지)."""
        server = self.app.server
        if server is None:
            return None, "프록시가 실행 중이 아닙니다"
        try:
            result = server.call_tool(tool, arguments)
        except Exception as exc:
            logging.getLogger("ui").exception("%s 호출 실패", tool)
            return None, f"{tool} 호출 실패: {exc}"
        if isinstance(result, dict) and "error" in result:
            error = result["error"]
            return None, f"{error.get('code')}: {error.get('message')}"
        return result, None

    def _call_async(self, tool, then, **arguments):
        """도구 호출을 워커로 보낸다. then(result, message)는 UI 스레드에서 실행된다.

        이 페이지의 호출은 전부 DB를 친다 — UI 스레드에서 하면 창이 언다.
        """
        def work():
            self.app.enqueue_ui_done(then, *self._call(tool, **arguments))

        threading.Thread(target=work, daemon=True).start()

    def _table_candidates(self, text):
        """자동완성 provider. 캐시가 있으면 즉시 거른 목록, 없으면 비동기 적재 후 None.

        사용자가 테이블 칸에 타이핑할 때만 불린다 — 페이지 진입·프로그램적
        값 설정으로는 DB를 치지 않는다 (자동 테이블 탐색 금지 계약 유지).
        """
        conn = self.cc_connection.get().strip()
        if not conn:
            return []
        if conn in self._table_cache:
            names = self._table_cache[conn]
            key = text.upper()
            return [n for n in names if key in n] if key else list(names)
        if conn not in self._tables_loading:
            self._tables_loading.add(conn)
            self._call_async(
                "list_tables",
                lambda result, message, c=conn: self._on_tables_loaded(c, result, message),
                connection=conn, limit=500)
        return None  # 적재 중 — 완료되면 refresh()로 다시 그린다

    def _on_tables_loaded(self, conn, result, message):
        self._tables_loading.discard(conn)  # 실패는 캐시하지 않는다 — 다음 타이핑에 재시도
        if result is None:
            self.cc_autocomplete.close()
            self._set_status(f"테이블 목록 조회 실패: {message}", "err")
            return
        self._table_cache[conn] = sorted(
            f"{t.get('owner')}.{t.get('name')}"
            for t in result.get("tables") or [] if isinstance(t, dict))
        # 사용자가 아직 테이블 칸에 있을 때만 팝업을 갱신한다.
        if self.cc_connection.get().strip() == conn and self.focus_get() is self.cc_table_entry:
            self.cc_autocomplete.refresh()

    def refresh_connections(self):
        """config 변경 후 커넥션 목록·폼·현재 등록 표시를 config.json 기준으로 되돌린다."""
        self._table_cache = {}  # 커넥션/서버가 바뀌었으니 자동완성 캐시 무효화
        self._tables_loading = set()
        self.cc_autocomplete.close()
        names = self._connection_names()
        self.cc_conn_combo.config(values=names)
        saved = self.app.config_data.get("common_code") or {}
        self.cc_connection.set(saved.get("connection") or (names[0] if names else ""))
        self.cc_table.set(saved.get("table", ""))
        if saved.get("table"):
            self.cc_current.set(f"{saved.get('connection', '')}  ·  {saved['table']}")
            self.cc_current_label.config(fg=P["text"])
        else:
            self.cc_current.set("등록된 테이블 없음")
            self.cc_current_label.config(fg=P["muted"])
        self._set_status("")

    def _block(self):
        """폼 → common_code 블록. 형식 오류는 ValueError."""
        connection = self.cc_connection.get().strip()
        table = self.cc_table.get().strip().upper()
        if not connection:
            raise ValueError("커넥션을 선택하세요.")
        if table.count(".") != 1:
            raise ValueError("테이블은 OWNER.TABLE 형식으로 입력하세요.")
        return {"connection": connection, "table": table}

    def _candidate_config(self):
        data = json.loads(json.dumps(self.app.config_data))
        data["common_code"] = self._block()
        config_store.validate(data)
        return data

    def _verify(self):
        """저장된 설정으로 프록시가 그 테이블을 알리고 읽을 수 있는지 확인한다."""
        try:
            block = self._block()
        except ValueError as exc:
            self._set_status(f"확인 실패: {exc}", "err")
            return
        if (self.app.config_data.get("common_code") or {}) != block:
            self._set_status("입력값이 아직 저장되지 않았습니다 · 먼저 [저장]하세요", "warn")
            return
        self._set_status("확인 중…")
        self._call_async("list_connections",
                         lambda listed, message: self._on_listed(block, listed, message))

    def _on_listed(self, block, listed, message):
        if listed is None:
            self._set_status(f"확인 실패: {message}", "err")
            return
        if not (listed.get("common_code") or {}).get("table"):
            self._set_status("설정이 아직 적용되지 않았습니다 · 다시 저장해 보세요", "err")
            return
        self._call_async("sample_rows", self._on_sampled,
                         connection=block["connection"], table=block["table"], limit=1)

    def _on_sampled(self, sampled, message):
        if sampled is None:
            self._set_status(f"확인 실패: {message}", "err")
            return
        self._set_status(f"조회 정상 · 컬럼 {len(sampled.get('columns') or ())}개 확인", "ok")

    def _save(self):
        try:
            data = self._candidate_config()
        except (ValueError, KeyError) as exc:
            self._set_status(f"저장 실패: {exc}", "err")
            return
        self._set_status("저장 중…")
        self.app.save_config(data, self._after_save)

    def _remove(self):
        if not self.app.config_data.get("common_code"):
            self._set_status("등록된 테이블이 없습니다.", "warn")
            return
        data = json.loads(json.dumps(self.app.config_data))
        data.pop("common_code", None)
        config_store.validate(data)
        self._set_status("해제 중…")
        self.app.save_config(data, self._after_save)

    def _after_save(self, ok, message):
        if not ok:
            self._set_status(message, "err")
            return
        self.app.pages["config"].revert()
        # 저장은 프록시를 재기동한다. 재기동된 카탈로그는 pending으로 시작하므로
        # 바로 조회하면 UNKNOWN_OBJECT가 난다. 준비될 때까지 after()로 기다린다.
        self._set_status("저장됨 · 적용 확인 중…")
        self._when_catalog_ready(self._post_save_refresh, attempts=40)

    def _when_catalog_ready(self, then, attempts):
        state = None
        server = self.app.server
        if server is not None:
            try:
                state = server.readiness().get("catalog")
            except Exception:
                logging.getLogger("ui").exception("readiness 조회 실패")
        if state == "pending" and attempts > 0:
            self.after(300, lambda: self._when_catalog_ready(then, attempts - 1))
            return
        then()

    def _post_save_refresh(self):
        self.app.on_config_saved()
        if self.app.config_data.get("common_code"):
            self._verify()
        else:
            self._set_status("등록 해제됨", "ok")


# ------------------------------------------------------------- 설정 페이지


def _as_int(raw):
    """폼 문자열을 int로. 실패하면 원문을 돌려줘 config_store.validate가 한국어로 거절하게 한다."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return raw


class ConfigPage(tk.Frame):
    """실제 연동: config/store.py — 검증→백업→원자적 저장→프록시 리로드.

    기본은 폼 편집. 폼은 proxy.port/log_level과 connections만 다루고, 그 외 키
    (ui, common_code, 커넥션의 알 수 없는 키)는 저장 시 그대로 보존한다.
    [JSON 편집]으로 파일 원본을 직접 고칠 수도 있다.
    """

    CONN_COLS = ("name", "dsn", "user", "pool")
    CONN_FIELDS = ("name", "dsn", "user", "password", "pool_max")

    def __init__(self, master, app):
        super().__init__(master, bg=P["bg"])
        self.app = app
        self._conns = []       # 폼 모델: connections 사본 (편집 중 값은 문자열일 수 있다)
        self._sel = None       # 선택된 커넥션 인덱스
        self._loading = False  # 폼을 채우는 동안 trace 콜백 무시
        self._mode = "form"

        holder = tk.Frame(self, bg=P["bg"])
        holder.pack(fill="both", expand=True)
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.form_view = tk.Frame(holder, bg=P["bg"])
        self.form_view.grid(row=0, column=0, sticky="nsew")
        self.json_view = card(holder)
        self.json_view.grid(row=0, column=0, sticky="nsew")
        self._build_form(self.form_view)
        self._build_json(self.json_view)

        bar = tk.Frame(self, bg=P["bg"])
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="저장", style="Accent.TButton", command=self._save).pack(side="left")
        ttk.Button(bar, text="되돌리기", style="Ghost.TButton", command=self.revert).pack(
            side="left", padx=6)
        self.mode_btn = ttk.Button(bar, text="JSON 편집", style="Ghost.TButton",
                                   command=self._toggle_mode)
        self.mode_btn.pack(side="left")
        self.status_var = tk.StringVar()
        self.status_label = tk.Label(bar, textvariable=self.status_var, bg=P["bg"],
                                     fg=P["muted"], font=FONTS["small"])
        self.status_label.pack(side="right")

        self.revert()  # 파일이 깨져 있으면 revert가 JSON 뷰로 전환한다
        (self.form_view if self._mode == "form" else self.json_view).tkraise()

    # ---------------------------------------------------------------- 뷰 구성

    def _build_form(self, parent):
        proxy_card = card(parent)
        proxy_card.pack(fill="x")
        card_header(proxy_card, "프록시", lambda head: tk.Label(
            head, text=str(self.app.config_path), bg=P["card"], fg=P["muted"],
            font=FONTS["small"]))
        pform = tk.Frame(proxy_card, bg=P["card"])
        pform.pack(fill="x", padx=12, pady=(0, 10))
        self.port_var = tk.StringVar()
        self.level_var = tk.StringVar()
        tk.Label(form_row(pform, "호스트"), text="127.0.0.1 (로컬 전용)", bg=P["card"],
                 fg=P["muted"], font=FONTS["body"]).pack(side="left")
        form_entry(pform, "포트", self.port_var)
        form_combo(pform, "로그 레벨", self.level_var, config_store.LOG_LEVELS,
                   state="readonly")

        conn_card = card(parent)
        conn_card.pack(fill="both", expand=True, pady=(10, 0))

        def conn_buttons(head):
            box = tk.Frame(head, bg=P["card"])
            ttk.Button(box, text="추가", style="Ghost.TButton",
                       command=self._add_conn).pack(side="left")
            ttk.Button(box, text="삭제", style="Ghost.TButton",
                       command=self._del_conn).pack(side="left", padx=(6, 0))
            return box

        card_header(conn_card, "커넥션", conn_buttons)
        self.conn_tree = scrolled(
            conn_card,
            lambda box: ttk.Treeview(box, columns=self.CONN_COLS, show="headings",
                                     style="Card.Treeview", height=4),
            fill="both", expand=True, padx=12)
        for col, head, w, a in zip(self.CONN_COLS, ("이름", "DSN", "사용자", "풀"),
                                   (110, 240, 110, 50), ("w", "w", "w", "e")):
            self.conn_tree.heading(col, text=head, anchor=a)
            self.conn_tree.column(col, width=w, minwidth=w, anchor=a, stretch=(a == "w"))
        self.conn_tree.bind("<<TreeviewSelect>>", self._on_conn_select)

        cform = tk.Frame(conn_card, bg=P["card"])
        cform.pack(fill="x", padx=12, pady=(6, 2))
        # anchor="n": 행 수가 달라도 두 컬럼의 첫 행 시작점(위)을 맞춘다.
        left = tk.Frame(cform, bg=P["card"])
        left.pack(side="left", fill="x", expand=True, anchor="n")
        right = tk.Frame(cform, bg=P["card"])
        right.pack(side="left", fill="x", expand=True, padx=(16, 0), anchor="n")
        self.conn_vars = {key: tk.StringVar() for key in self.CONN_FIELDS}
        form_entry(left, "이름", self.conn_vars["name"])
        form_entry(left, "DSN", self.conn_vars["dsn"])
        form_entry(left, "사용자", self.conn_vars["user"])
        form_entry(right, "암호", self.conn_vars["password"], show="•")
        form_entry(right, "풀 최대", self.conn_vars["pool_max"])
        for key, var in self.conn_vars.items():
            var.trace_add("write", lambda *_a, k=key: self._on_conn_edit(k))
        tk.Frame(conn_card, bg=P["card"], height=8).pack(fill="x")

    def _build_json(self, parent):
        card_header(parent, "config.json 원본")
        self.editor = scrolled(
            parent,
            lambda box: tk.Text(box, font=FONTS["mono"], bg=P["field"], fg=P["text"],
                                bd=0, highlightthickness=0, undo=True, padx=10, pady=8,
                                insertbackground=P["text"]),
            fill="both", expand=True, padx=12, pady=(0, 12))
        force_english_input(self.editor)

    # ------------------------------------------------------------- 폼 모델

    def _row_values(self, conn):
        return (conn.get("name", ""), conn.get("dsn", ""), conn.get("user", ""),
                str(conn.get("pool_max", "")))

    def _load_model(self, data):
        proxy = (data or {}).get("proxy") or {}
        self.port_var.set(str(proxy.get("port", "")))
        self.level_var.set(str(proxy.get("log_level", "INFO")))
        conns = (data or {}).get("connections") or []
        self._conns = [dict(c) for c in conns if isinstance(c, dict)]
        self._rebuild_tree(select=0)

    def _rebuild_tree(self, select=None):
        self._sel = None
        self.conn_tree.delete(*self.conn_tree.get_children())
        for i, conn in enumerate(self._conns):
            self.conn_tree.insert("", "end", iid=str(i), values=self._row_values(conn))
        if self._conns and select is not None:
            idx = min(select, len(self._conns) - 1)
            self.conn_tree.selection_set(str(idx))
            self.conn_tree.focus(str(idx))
            # <<TreeviewSelect>>는 비동기로 온다 — 폼은 지금 채워야 직후 편집이 유실되지 않는다.
            self._fill_conn_form(idx)
        else:
            self._fill_conn_form(None)

    def _on_conn_select(self, _event=None):
        selection = self.conn_tree.selection()
        self._fill_conn_form(int(selection[0]) if selection else None)

    def _fill_conn_form(self, idx):
        self._loading = True
        try:
            conn = self._conns[idx] if idx is not None else {}
            for key, var in self.conn_vars.items():
                var.set(str(conn.get(key, "")))
            self._sel = idx
        finally:
            self._loading = False

    def _on_conn_edit(self, key):
        if self._loading or self._sel is None:
            return
        self._conns[self._sel][key] = self.conn_vars[key].get()
        self.conn_tree.item(str(self._sel), values=self._row_values(self._conns[self._sel]))

    def _add_conn(self):
        self._conns.append({"name": f"conn{len(self._conns) + 1}", "dsn": "", "user": "",
                            "password": "", "pool_max": 5})
        self._rebuild_tree(select=len(self._conns) - 1)

    def _del_conn(self):
        if self._sel is None:
            return
        removed = self._sel
        del self._conns[removed]
        self._rebuild_tree(select=removed if self._conns else None)

    def _form_data(self):
        """폼 → config dict. proxy/connections 외 최상위 키와 커넥션의 미지 키는 보존."""
        data = json.loads(json.dumps(self.app.config_data))
        data["proxy"] = dict(data.get("proxy") or {})
        data["proxy"].update(host="127.0.0.1", port=_as_int(self.port_var.get()),
                             log_level=self.level_var.get().strip())
        conns = []
        for item in self._conns:
            conn = dict(item)
            conn["pool_max"] = _as_int(conn.get("pool_max", ""))
            for key in ("name", "dsn", "user", "password_env", "password"):
                if isinstance(conn.get(key), str):
                    conn[key] = conn[key].strip()
            for key in ("password", "password_env"):
                if not conn.get(key):
                    conn.pop(key, None)
            conns.append(conn)
        data["connections"] = conns
        return data

    # ------------------------------------------------------------- 동작

    def _set_status(self, text, kind="muted"):
        self.status_var.set(text)
        self.status_label.config(fg=P.get(kind, P["muted"]))

    def _parse(self):
        if self._mode == "form":
            data = self._form_data()
        else:
            try:
                data = json.loads(self.editor.get("1.0", "end-1c"))
            except json.JSONDecodeError as exc:
                self._set_status(f"✘ JSON 오류: {exc.lineno}행 {exc.colno}열 — {exc.msg}", "err")
                return None
        try:
            config_store.validate(data)
        except ValueError as exc:
            self._set_status(f"✘ {exc}", "err")
            return None
        return data

    def _toggle_mode(self):
        if self._mode == "form":
            set_text(self.editor, json.dumps(self._form_data(), indent=2, ensure_ascii=False))
            self._mode = "json"
            self.mode_btn.config(text="폼 편집")
            self.json_view.tkraise()
            return
        try:
            data = json.loads(self.editor.get("1.0", "end-1c"))
        except json.JSONDecodeError as exc:
            self._set_status(f"✘ JSON 오류: {exc.lineno}행 {exc.colno}열 — {exc.msg}", "err")
            return
        self._load_model(data)
        self._mode = "form"
        self.mode_btn.config(text="JSON 편집")
        self.form_view.tkraise()

    def _save(self):
        data = self._parse()
        if data is None:
            return
        self._set_status("저장 중…")
        try:
            self.app.save_config(data, self._on_saved)
        except OSError as exc:
            self._set_status(f"✘ 파일 저장 실패: {exc}", "err")
        except ValueError as exc:
            self._set_status(f"✘ {exc}", "err")

    def _on_saved(self, ok, message):
        self._set_status(message, "ok" if ok else "err")
        if ok:
            self.app.on_config_saved()

    def revert(self):
        text = Path(self.app.config_path).read_text(encoding="utf-8-sig")
        set_text(self.editor, text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 밖에서 파일이 깨진 채로 편집됐다 — 원본 편집으로 열어 고칠 수 있게 한다.
            self._mode = "json"
            self.mode_btn.config(text="폼 편집")
            self.json_view.tkraise()
            self._set_status("파일이 올바른 JSON이 아니라 원본 편집으로 엽니다", "warn")
            return
        self._load_model(data)
        self._set_status("파일 내용을 다시 불러왔습니다")


# ------------------------------------------------------------- 로그 페이지


class LogPage(tk.Frame):
    """실제 연동: logging QueueHandler → queue.Queue → after() 폴링 렌더."""

    LEVELS = ("ALL", "DEBUG", "INFO", "WARNING", "ERROR")
    # 경고/오류만 고정색이다 — 콘솔은 모든 팔레트에서 다크라 라이트 카드용
    # P["warn"]/P["err"]를 쓰면 대비가 무너진다. 나머지는 팔레트를 따른다.
    FIXED_COLORS = {"WARNING": "#F5B84B", "ERROR": "#F0716B"}
    MAX_BUFFER = 5000

    def __init__(self, master, app):
        super().__init__(master, bg=P["bg"])
        self.log_queue = app.log_queue
        self.buffer = []
        self.paused = False
        self.level = "ALL"

        c = card(self)
        c.pack(fill="both", expand=True)
        bar = tk.Frame(c, bg=P["card"])
        bar.pack(fill="x", padx=12, pady=10)
        Chips(bar, self.LEVELS, self._on_level, bg=P["card"]).pack(side="left")
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="자동 스크롤", variable=self.autoscroll,
                        style="Card.TCheckbutton").pack(side="right")
        self.pause_btn = ttk.Button(bar, text="일시정지", style="Ghost.TButton",
                                    command=self._toggle_pause)
        self.pause_btn.pack(side="right", padx=8)
        ttk.Button(bar, text="지우기", style="Ghost.TButton", command=self._clear).pack(side="right")

        self.text = scrolled(
            c,
            lambda box: tk.Text(box, font=FONTS["mono_s"], bg=P["console"],
                                fg=P["console_fg"], bd=0, highlightthickness=0,
                                padx=10, pady=8, state="disabled"),
            dark=True, fill="both", expand=True, padx=12)
        colors = {"DEBUG": P["muted"], "INFO": P["console_fg"], **self.FIXED_COLORS}
        for level, color in colors.items():
            self.text.tag_config(level, foreground=color)

        foot = tk.Frame(c, bg=P["card"])
        foot.pack(fill="x", padx=12, pady=(4, 8))
        self.count_var = tk.StringVar()
        tk.Label(foot, textvariable=self.count_var, bg=P["card"], fg=P["muted"],
                 font=FONTS["small"]).pack(side="right")
        self._poll()

    def _on_level(self, level):
        self.level = level
        self._rerender()

    def _toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="재개" if self.paused else "일시정지")
        if not self.paused:
            self._rerender()

    def _clear(self):
        self.buffer.clear()
        self._rerender()

    def _visible(self, level):
        return self.level == "ALL" or level == self.level

    def _append_line(self, level, line, scroll=True):
        self.text.config(state="normal")
        self.text.insert("end", line + "\n", level)
        self.text.config(state="disabled")
        if scroll and self.autoscroll.get():
            self.text.see("end")

    def _rerender(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        for level, line in self.buffer:
            if self._visible(level):
                self._append_line(level, line, scroll=False)  # 스크롤은 끝에서 한 번만
        if self.autoscroll.get():
            self.text.see("end")
        self._update_count()

    def _update_count(self):
        shown = int(self.text.index("end-1c").split(".")[0]) - 1
        self.count_var.set(f"{shown:,} / {len(self.buffer):,}행")

    def _poll(self):
        while True:
            try:
                level, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.buffer.append((level, line))
            if len(self.buffer) > self.MAX_BUFFER:
                del self.buffer[: len(self.buffer) - self.MAX_BUFFER]
            if not self.paused and self._visible(level):
                self._append_line(level, line)
        self._update_count()
        self.after(200, self._poll)


# ------------------------------------------------------------- 사이드바/앱

# 아이콘은 Segoe UI Symbol 커버리지가 확실한 모노크롬 글리프만 쓴다 (이미지 반입 없음).
NAV = [
    ("status", "◉", "상태"),
    ("claude", "⇄", "Claude 연동"),
    ("tools", "⚒", "도구"),
    ("extensions", "⊞", "공통코드"),
    ("config", "⚙", "설정"),
    ("logs", "▤", "로그"),
]


class Sidebar(tk.Frame):
    """좌측 내비게이션 + 프록시 실행 상태 표시. 페이지 전환은 App._show_page 로 위임."""

    # 다크 사이드바 위 상태색 — 라이트 카드용 P["warn"]/P["err"]는 대비가 무너진다.
    STATE_COLORS = {"warn": "#F5B84B", "err": "#F0716B"}

    def __init__(self, master, on_select):
        super().__init__(master, bg=P["sidebar"], width=170)
        self.pack_propagate(False)
        self.on_select = on_select
        self.items = {}
        self.active = None

        logo = tk.Frame(self, bg=P["sidebar"])
        logo.pack(fill="x", pady=(14, 10), padx=14)
        tk.Label(logo, text="⛁", bg=P["sidebar"], fg=P["accent"],
                 font=(FONTS["title"][0], 15)).pack(side="left")
        tk.Label(logo, text=" DBridge", bg=P["sidebar"], fg="#FFFFFF",
                 font=FONTS["h2"]).pack(side="left")

        for key, icon, label in NAV:
            self.items[key] = self._nav_item(key, icon, label)

        bottom = tk.Frame(self, bg=P["sidebar"])
        bottom.pack(side="bottom", fill="x", pady=10, padx=14)
        self.run_label = tk.Label(bottom, text="● 중지됨", bg=P["sidebar"],
                                  fg=self.STATE_COLORS["err"], font=FONTS["small_b"])
        self.run_label.pack(anchor="w")
        tk.Label(bottom, text=f"v{__version__}", bg=P["sidebar"], fg=P["side_text"],
                 font=FONTS["small"]).pack(anchor="w", pady=(2, 0))

    def _nav_item(self, key, icon, label):
        row = tk.Frame(self, bg=P["sidebar"], cursor="hand2")
        row.pack(fill="x")
        indicator = tk.Frame(row, bg=P["sidebar"], width=3)
        indicator.pack(side="left", fill="y")
        # 아이콘은 고정폭 컬럼 — 글리프 폭이 제각각이어도 라벨 시작점이 맞는다.
        icon_lb = tk.Label(row, text=icon, bg=P["sidebar"], fg=P["side_text"],
                           font=FONTS["body"], width=3, pady=6)
        icon_lb.pack(side="left")
        text = tk.Label(row, text=label, bg=P["sidebar"], fg=P["side_text"],
                        font=FONTS["body"], anchor="w", pady=6)
        text.pack(side="left", fill="x", expand=True)

        widgets = (row, icon_lb, text)
        for w in widgets:
            w.bind("<Button-1>", lambda e, k=key: self.select(k))
            w.bind("<Enter>", lambda e, k=key: self._hover(k, True))
            w.bind("<Leave>", lambda e, k=key: self._hover(k, False))
        return {"row": row, "indicator": indicator, "icon": icon_lb, "text": text}

    def _hover(self, key, on):
        if key == self.active:
            return
        bg = P["sidebar_hover"] if on else P["sidebar"]
        item = self.items[key]
        for w in (item["row"], item["icon"], item["text"], item["indicator"]):
            w.config(bg=bg)

    def select(self, key):
        if self.active and self.active in self.items:
            old = self.items[self.active]
            for w in (old["row"], old["icon"], old["text"], old["indicator"]):
                w.config(bg=P["sidebar"])
            old["icon"].config(fg=P["side_text"])
            old["text"].config(fg=P["side_text"])
        self.active = key
        item = self.items[key]
        item["row"].config(bg=P["sidebar_active"])
        item["icon"].config(bg=P["sidebar_active"], fg=P["side_text_hi"])
        item["text"].config(bg=P["sidebar_active"], fg=P["side_text_hi"])
        item["indicator"].config(bg=P["accent"])
        self.on_select(key)

    def set_running(self, text, kind):
        color = P["side_ok"] if kind == "ok" else self.STATE_COLORS.get(kind, P["side_text"])
        self.run_label.config(text=text, fg=color)


class App(tk.Tk):
    """Tk 루트: 페이지 라우터 + ProxyServer 수명주기 소유자.

    UI 스레드에서 DB·소켓 작업을 하지 않는다 — 저장/재기동/테스트는 워커
    스레드로 보내고 결과를 ``enqueue_ui_done`` 으로 Tk 큐에 되돌린다.
    ``server`` 프로퍼티가 항상 최신 인스턴스를 가리키므로 페이지는 그것만 읽는다.
    """
    def __init__(self, server, config, config_path, log_queue, *, create=None):
        super().__init__()
        self.config_data = config
        self.config_path = Path(config_path)
        self.log_queue = log_queue
        self._ui_done_queue: queue.Queue = queue.Queue()
        base_dir = self.config_path.parent
        # create: config -> ProxyServer. 기본은 실 DB. app.py --fake는 fake DB 주입 팩토리를 넘긴다.
        factory = create if create is not None else (lambda cfg: create_server(cfg, base_dir=base_dir))
        self.lifecycle = ServerLifecycle(server, factory)
        self.title(APP_TITLE)
        self.geometry("940x640")
        self.minsize(840, 560)
        init_fonts(self)
        setup_styles(self)
        self.configure(bg=P["bg"])

        self.sidebar = Sidebar(self, self._show_page)
        self.sidebar.pack(side="left", fill="y")

        main = tk.Frame(self, bg=P["bg"])
        main.pack(side="left", fill="both", expand=True)

        # 헤더 — 서버 상태는 사이드바 하단(버전 위)에 있어 여기는 제목만 둔다.
        header = tk.Frame(main, bg=P["bg"])
        header.pack(fill="x", padx=16, pady=(12, 8))
        self.page_title = tk.Label(header, text="", bg=P["bg"], fg=P["text"], font=FONTS["title"])
        self.page_title.pack(side="left")

        # 페이지 라우터
        container = tk.Frame(main, bg=P["bg"])
        container.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.pages = {
            "status": StatusPage(container, self),
            "claude": ClaudePage(container, self),
            "tools": ToolsPage(container, self),
            "extensions": ExtensionPage(container, self),
            "config": ConfigPage(container, self),
            "logs": LogPage(container, self),
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self.sidebar.select("status")
        if sys.platform == "darwin":
            self.after_idle(self._present_macos_window)
        self._pump_ui_done()

    def enqueue_ui_done(self, callback, *args):
        """워커 스레드에서 호출. self.after(0, ...) 대신 큐에 넣고 메인 스레드 펌프가 소비한다
        (Tk의 after()는 스레드 세이프하지 않아 콜백이 유실될 수 있다; 로그 피드와 같은 패턴).
        """
        self._ui_done_queue.put((callback, args))

    def _pump_ui_done(self):
        try:
            while True:
                try:
                    callback, args = self._ui_done_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback(*args)
                except Exception:
                    logging.getLogger("ui").exception("UI 완료 콜백 예외")
        finally:
            self.after(50, self._pump_ui_done)

    def _show_page(self, key):
        titles = {k: label for k, _i, label in NAV}
        self.page_title.config(text=titles[key])
        page = self.pages[key]
        page.tkraise()
        on_show = getattr(page, "on_show", None)
        if callable(on_show):
            on_show()

    def set_running(self, text, kind):
        self.sidebar.set_running(text, kind)

    @property
    def server(self):
        return self.lifecycle.current()

    def report_callback_exception(self, exc, val, tb):
        """Tk 콜백에서 새는 예외를 로그로 남긴다 (창을 죽이지 않는다)."""
        logging.getLogger("ui").exception("UI 콜백 예외", exc_info=(exc, val, tb))

    def save_config(self, data, on_done):
        """검증→백업→원자적 저장→프록시 재기동을 워커 스레드에서 수행한다.

        ``on_done(ok, message)`` 는 Tk 스레드에서 호출된다.
        """
        config_store.save(self.config_path, data)
        self.config_data.clear()
        self.config_data.update(data)
        logging.getLogger().setLevel(data["proxy"]["log_level"])
        snapshot = json.loads(json.dumps(data))
        seq = self.lifecycle.next_seq()

        def work():
            try:
                self.lifecycle.reload(snapshot, seq=seq)
                ok, message = True, "✔ 저장 완료 · 변경 사항이 적용되었습니다"
            except Exception as extra:
                logging.getLogger("proxy").exception("프록시 재기동 실패")
                ok, message = False, f"✘ 저장은 됐지만 적용에 실패했습니다: {extra}"
            self.enqueue_ui_done(on_done, ok, message)

        threading.Thread(target=work, daemon=True).start()

    def close(self):
        self.lifecycle.close()

    def on_config_saved(self):
        """설정 저장 후 각 페이지를 새 config/서버 기준으로 갱신한다."""
        self.pages["claude"].refresh()
        self.pages["extensions"].refresh_connections()
        self.pages["tools"].refresh()
        self.pages["status"].refresh()

    def _present_macos_window(self):
        """터미널에서 실행한 system Tk 창이 뒤나 화면 밖에 머무르지 않게 표시한다."""
        self.update_idletasks()
        width = max(self.winfo_width(), 940)
        height = max(self.winfo_height(), 640)
        x = max((self.winfo_screenwidth() - width) // 2, 0)
        y = max((self.winfo_screenheight() - height) // 2, 0)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        self.lift()
        try:
            self.attributes("-topmost", True)
            self.after(250, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except tk.TclError:
            pass

    def tool_catalog(self):
        """실행 중 서버의 tools() 를 UI 표시용 카탈로그로 변환한다."""
        return load_tool_catalog(self.server)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            set_palette(sys.argv[1])
        except ValueError as exc:
            print(exc)
            sys.exit(1)
    raise SystemExit("실행 진입점은 app.py 입니다: python app.py (팔레트는 config.json의 ui.palette)")
