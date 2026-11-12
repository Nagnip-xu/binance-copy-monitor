"""Scrollable settings tabs that do not dictate the height of the whole window."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class SettingsTab(ttk.Frame):
    def __init__(self, notebook: ttk.Notebook, title: str):
        super().__init__(notebook, style="Panel.TFrame")
        self.notebook = notebook
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        background = ttk.Style(notebook).lookup("Panel.TFrame", "background") or "#FFFFFF"
        self.canvas = tk.Canvas(self, height=1, highlightthickness=0, bd=0, bg=background)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = ttk.Frame(self.canvas, style="Panel.TFrame", padding=14)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._content_changed)
        self.canvas.bind("<Configure>", self._viewport_changed)
        self.content.bind("<FocusIn>", self._focus_visible, add=True)
        self.bind_all("<MouseWheel>", self._wheel, add=True)
        self.bind_all("<FocusIn>", self._focus_visible, add=True)
        notebook.add(self, text=title)

    def _content_changed(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._scrollbar()
        self.notebook.event_generate("<<SettingsContentChanged>>", when="tail")

    def _viewport_changed(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._scrollbar()

    def _scrollbar(self):
        overflow = self.content.winfo_reqheight() > self.canvas.winfo_height()
        if overflow and not self.scrollbar.winfo_manager():
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        elif not overflow and self.scrollbar.winfo_manager():
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _contains(self, widget):
        while widget is not None:
            if widget is self.content:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _wheel(self, event):
        if self.notebook.select() != str(self) or not self.scrollbar.winfo_manager():
            return
        widget = self.winfo_containing(event.x_root, event.y_root)
        if self._contains(widget) and event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

    def _focus_visible(self, event):
        if self.notebook.select() != str(self) or not self._contains(event.widget):
            return
        # Tab/Shift+Tab must reveal controls below the settings viewport too.
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        y = event.widget.winfo_rooty() - self.content.winfo_rooty()
        end = y + event.widget.winfo_height()
        height = max(1, self.content.winfo_height())
        if y < top:
            self.canvas.yview_moveto(max(0, y - 8) / height)
        elif end > bottom:
            self.canvas.yview_moveto(max(0, end - self.canvas.winfo_height() + 8) / height)


def settings_tab(notebook: ttk.Notebook, title: str) -> ttk.Frame:
    return SettingsTab(notebook, title).content
