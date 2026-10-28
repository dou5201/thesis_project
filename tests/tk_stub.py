"""A strict stand-in for tkinter used to smoke-test the GUI where no display / Tk library exists.

Widgets accept only the option names that real Tk accepts (so typos raise errors like Tk would),
Variables hold values, `after` callbacks run on a simulated clock, dialogs return scripted answers.
The matplotlib Tk canvas is replaced by an Agg canvas so every figure is really rendered.
"""
import sys
import time
import types

COMMON = {"class_", "cursor", "style", "takefocus", "name"}
OPTS = {
    "Frame": {"padding", "relief", "borderwidth", "width", "height"},
    "LabelFrame": {"padding", "relief", "borderwidth", "width", "height", "text", "labelanchor", "labelwidget", "underline"},
    "Label": {"anchor", "background", "font", "foreground", "justify", "padding", "relief", "text", "textvariable",
              "underline", "width", "wraplength", "image", "compound", "borderwidth"},
    "Button": {"command", "default", "width", "text", "textvariable", "underline", "image", "compound", "padding", "state"},
    "Radiobutton": {"command", "value", "variable", "text", "textvariable", "underline", "width", "image", "compound",
                    "padding", "state"},
    "Checkbutton": {"command", "offvalue", "onvalue", "variable", "text", "textvariable", "underline", "width", "image",
                    "compound", "padding", "state"},
    "Entry": {"exportselection", "invalidcommand", "justify", "show", "state", "textvariable", "validate",
              "validatecommand", "width", "font", "foreground", "background"},
    "Scale": {"command", "from_", "length", "orient", "to", "value", "variable", "state"},
    "Notebook": {"height", "padding", "width"},
    "Treeview": {"columns", "displaycolumns", "height", "padding", "selectmode", "show", "xscrollcommand", "yscrollcommand"},
    "Text": {"width", "height", "wrap", "font", "relief", "background", "foreground", "state", "padx", "pady",
             "borderwidth", "insertbackground", "highlightthickness", "bg", "fg"},
    "Tk": set(),
}
OPTS["Combobox"] = OPTS["Entry"] | {"height", "postcommand", "values"}
OPTS["Spinbox"] = OPTS["Entry"] | {"from_", "to", "increment", "values", "wrap", "format", "command"}
PACK = {"side", "fill", "expand", "padx", "pady", "anchor", "ipadx", "ipady", "before", "after", "in_"}
GRID = {"row", "column", "rowspan", "columnspan", "sticky", "padx", "pady", "ipadx", "ipady", "in_"}
STATES = {"disabled", "!disabled", "readonly", "!readonly", "focus", "!focus", "active", "!active"}


class TclError(Exception):
    pass


def _check(kind, kw, allowed):
    bad = set(kw) - allowed
    if bad:
        raise TclError(f'{kind}: unknown option(s) {sorted(bad)}')


class Widget:
    KIND = "Frame"

    def __init__(self, master=None, **kw):
        _check(self.KIND, kw, OPTS[self.KIND] | COMMON)
        self.master = master
        self.options = dict(kw)
        self.states = set()
        self.children = []
        if master is not None and hasattr(master, "children"):
            master.children.append(self)

    def pack(self, **kw):
        _check("pack", kw, PACK)

    def grid(self, **kw):
        _check("grid", kw, GRID)
        if "row" not in kw or "column" not in kw:
            raise TclError("grid without row/column")

    def columnconfigure(self, index, **kw):
        _check("columnconfigure", kw, {"weight", "minsize", "pad", "uniform"})

    rowconfigure = columnconfigure

    def configure(self, **kw):
        _check(self.KIND + ".configure", kw, OPTS[self.KIND] | COMMON)
        self.options.update(kw)

    config = configure

    def cget(self, key):
        return self.options.get(key)

    def state(self, spec=None):
        if spec is None:
            return tuple(self.states)
        for s in spec:
            if s not in STATES:
                raise TclError(f"bad state {s}")
            if s.startswith("!"):
                self.states.discard(s[1:])
            else:
                self.states.add(s)
        return ()

    def instate(self, spec):
        return all((s[1:] not in self.states) if s.startswith("!") else (s in self.states) for s in spec)

    def bind(self, *a, **k):
        return "bind"

    def focus_set(self):
        pass

    def winfo_exists(self):
        return True


def _mk(kind):
    return type(kind, (Widget,), {"KIND": kind})


class Variable:
    _default = ""

    def __init__(self, master=None, value=None, name=None):
        self._v = self._default if value is None else value

    def set(self, v):
        self._v = v

    def get(self):
        return self._cast(self._v)

    @staticmethod
    def _cast(v):
        return v


class StringVar(Variable):
    _default = ""

    @staticmethod
    def _cast(v):
        return str(v)


class IntVar(Variable):
    _default = 0

    @staticmethod
    def _cast(v):
        return int(v)


class DoubleVar(Variable):
    _default = 0.0

    @staticmethod
    def _cast(v):
        return float(v)


class BooleanVar(Variable):
    _default = False

    @staticmethod
    def _cast(v):
        return bool(v)


class Tk(Widget):
    KIND = "Tk"

    def __init__(self, *a, **k):
        super().__init__(None)
        self._after = {}
        self._next = 0
        self.destroyed = False
        self.protocols = {}

    def title(self, t):
        self._title = t

    def geometry(self, g):
        pass

    def minsize(self, w, h):
        pass

    def protocol(self, name, func):
        self.protocols[name] = func

    def after(self, ms, func=None, *args):
        self._next += 1
        jid = f"after#{self._next}"
        self._after[jid] = (time.monotonic() + ms / 1000.0, func, args)
        return jid

    def after_cancel(self, jid):
        self._after.pop(jid, None)

    def run_pending(self):
        now = time.monotonic()
        due = sorted(((t, j) for j, (t, f, a) in self._after.items() if t <= now))
        for _t, jid in due:
            entry = self._after.pop(jid, None)
            if entry:
                entry[1](*entry[2])

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.run_pending()
            time.sleep(0.01)

    def mainloop(self):
        pass

    def destroy(self):
        self.destroyed = True


class Notebook(_mk("Notebook")):
    def add(self, child, **kw):
        _check("Notebook.add", kw, {"text", "padding", "sticky", "state", "image", "compound", "underline"})

    def select(self, tab=None):
        self.selected = tab


class Treeview(_mk("Treeview")):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._items = []
        self._sel = ()
        self._n = 0

    def heading(self, col, **kw):
        _check("heading", kw, {"text", "anchor", "command", "image"})

    def column(self, col, **kw):
        _check("column", kw, {"width", "minwidth", "stretch", "anchor", "id"})

    def insert(self, parent, index, **kw):
        _check("insert", kw, {"values", "text", "iid", "open", "tags", "image"})
        self._n += 1
        iid = f"I{self._n:03d}"
        self._items.append((iid, kw.get("values")))
        return iid

    def get_children(self, item=""):
        return tuple(i for i, _ in self._items)

    def delete(self, *items):
        self._items = [(i, v) for i, v in self._items if i not in items]

    def selection(self):
        return self._sel

    def selection_set(self, item):
        self._sel = (item,)

    def index(self, item):
        return [i for i, _ in self._items].index(item)


class Text(_mk("Text")):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.content = ""

    def insert(self, index, text):
        if self.options.get("state") == "disabled":
            return
        self.content += text

    def delete(self, a, b=None):
        if self.options.get("state") == "disabled":
            return
        self.content = ""

    def get(self, a, b=None):
        return self.content


class Style:
    def __init__(self, master=None):
        pass

    def theme_names(self):
        return ("clam", "default")

    def theme_use(self, name=None):
        return "clam"

    def configure(self, style, **kw):
        pass


class Dialogs:
    """Scripted answers for messagebox / filedialog."""
    def __init__(self):
        self.errors = []
        self.infos = []
        self.yesno_answers = []
        self.paths = []

    def showerror(self, title, msg, **k):
        self.errors.append((title, msg))

    def showinfo(self, title, msg, **k):
        self.infos.append((title, msg))

    showwarning = showinfo

    def askyesno(self, title, msg, **k):
        return self.yesno_answers.pop(0) if self.yesno_answers else True

    def asksaveasfilename(self, **k):
        return self.paths.pop(0) if self.paths else ""

    askopenfilename = asksaveasfilename


DIALOGS = Dialogs()


class FigureCanvasTkAgg:
    def __init__(self, figure, master=None):
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        self.figure = figure
        self.agg = FigureCanvasAgg(figure)
        self.draw_count = 0
        self._widget = Widget(master)

    def get_tk_widget(self):
        return self._widget

    def draw(self):
        self.agg.draw()
        self.draw_count += 1

    draw_idle = draw


class NavigationToolbar2Tk(Widget):
    KIND = "Frame"

    def __init__(self, canvas, window=None, pack_toolbar=True):
        super().__init__(window)

    def update(self):
        pass


def install():
    tk = types.ModuleType("tkinter")
    for name in ("Frame", "Label", "Button"):
        setattr(tk, name, _mk(name))
    tk.Tk, tk.Text, tk.TclError = Tk, Text, TclError
    tk.Variable, tk.StringVar, tk.IntVar, tk.DoubleVar, tk.BooleanVar = Variable, StringVar, IntVar, DoubleVar, BooleanVar
    for c, v in dict(LEFT="left", RIGHT="right", TOP="top", BOTTOM="bottom", X="x", Y="y", BOTH="both", END="end",
                     HORIZONTAL="horizontal", VERTICAL="vertical", W="w", E="e", N="n", S="s", NONE="none").items():
        setattr(tk, c, v)
    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Frame", "LabelFrame", "Label", "Button", "Radiobutton", "Checkbutton", "Entry", "Combobox",
                 "Spinbox", "Scale"):
        setattr(ttk, name, _mk(name))
    ttk.Notebook, ttk.Treeview, ttk.Style = Notebook, Treeview, Style
    mb = types.ModuleType("tkinter.messagebox")
    fd = types.ModuleType("tkinter.filedialog")
    for n in ("showerror", "showinfo", "showwarning", "askyesno"):
        setattr(mb, n, getattr(DIALOGS, n))
    for n in ("asksaveasfilename", "askopenfilename"):
        setattr(fd, n, getattr(DIALOGS, n))
    tk.ttk, tk.messagebox, tk.filedialog = ttk, mb, fd
    back = types.ModuleType("matplotlib.backends.backend_tkagg")
    back.FigureCanvasTkAgg, back.NavigationToolbar2Tk = FigureCanvasTkAgg, NavigationToolbar2Tk
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb, "tkinter.filedialog": fd,
                        "matplotlib.backends.backend_tkagg": back})
    return DIALOGS
