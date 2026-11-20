"""Application glyphs dwipe adds to the console-window registry."""
from console_window import Glyphs

_VOCAB = {
    'tree_tee': ('├', '|'),        # U+251C partition tree junction
    'tree_corner': ('└', '`'),     # U+2514 last partition / port line
    'divide': ('÷', '/'),          # U+00F7 slowdown ratio
    'delta': ('Δ', 'd'),           # U+0394 stall time
}


def register_glyphs():
    """Register dwipe's application glyphs (safe to call more than once)."""
    Glyphs.register_many(_VOCAB, probe=tuple(_VOCAB))
