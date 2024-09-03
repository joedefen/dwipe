#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Custom Wrapper for python curses.
"""
# pylint: disable=too-many-instance-attributes,too-many-arguments
# pylint: disable=invalid-name,broad-except,too-many-branches

# pylint: disable=wrong-import-position,disable=wrong-import-order
# import VirtEnv
# VirtEnv.ensure_venv(__name__)

import traceback
import atexit
import signal
import time
import curses
import textwrap
from types import SimpleNamespace
from curses.textpad import rectangle, Textbox
dump_str = None

def ignore_ctrl_c():
    """" Ignore SIGINT """
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def restore_ctrl_c():
    """" Handle SIGINT """
    signal.signal(signal.SIGINT, signal.default_int_handler)

class OptionSpinner:
    """Manage a bunch of options where the value is rotate thru
    a fixed set of values pressing a key."""
    def __init__(self):
        """Give the object with the attribute to change its
        value (e.g., options from argparse or "self" from
        the object managing the window).

        And array of specs like:
            ['a - allow auto suggestions', 'allow_auto', True, False],
            ['/ - filter pattern', 'filter_str', self.filter_str],
        A spec can have a trailing None + more comments shown after
        the value.
        """
        self.options, self.keys = [], []
        self.margin = 4 # + actual width (1st column right pos)
        self.align = self.margin # + actual width (1st column right pos)
        self.default_obj = SimpleNamespace() # if not given one
        self.attr_to_option = {} # given an attribute, find its option ns
        self.key_to_option = {} # given key, options namespace
        self.keys = set()

    @staticmethod
    def _make_option_ns():
        return SimpleNamespace(
            keys=[],
            descr='',
            obj=None,
            attr='',
            vals=None,
            prompt=None,
            comments=[],
        )

    def get_value(self, attr, coerce=False):
        """Get the value of the given attribute."""
        ns = self.attr_to_option.get(attr, None)
        obj = ns.obj if ns else None
        value = getattr(obj, attr, None) if obj else None
        if value is None and obj and coerce:
            if ns.vals:
                if value not in ns.vals:
                    value = ns.vals[0]
                    setattr(obj, attr, value)
            else:
                if value is None:
                    value = ''
                    setattr(ns.obj, ns.attr, '')
        return value

    def _register(self, ns):
        """ Create the mappings needed"""
        assert ns.attr not in self.attr_to_option
        self.attr_to_option[ns.attr] = ns
        for key in ns.keys:
            assert key not in self.key_to_option, f'key ({chr(key)}, {key}) already used'
            self.key_to_option[key] = ns
            self.keys.add(key)
        self.options.append(ns)
        self.align = max(self.align, self.margin+len(ns.descr))
        self.get_value(ns.attr, coerce=True)

    def add(self, obj, specs):
        """ Compatibility Method."""
        for spec in specs:
            ns = self._make_option_ns()
            ns.descr = spec[0]
            ns.obj = obj
            ns.attr = spec[1]
            ns.vals=spec[2:]
            if None in ns.vals:
                idx = ns.vals.index(None)
                ns.vals = ns.vals[:idx]
                ns.comments = ns.vals[idx+1:]
            ns.keys = [ord(ns.descr[0])]
            self._register(ns)

    def add_key(self, attr, descr, obj=None, vals=None, prompt=None, keys=None, comments=None):
        """ Standard method"""
        ns = self._make_option_ns()
        if keys:
            ns.keys = list(keys) if isinstance(keys, (list, tuple, set)) else [keys]
        else:
            ns.keys = [ord(descr[0])]
        if comments is None:
            ns.comments = []
        else:
            ns.comments = list(comments) if isinstance(keys, (list, tuple)) else [comments]
        ns.descr = descr
        ns.attr = attr
        ns.obj = obj if obj else self.default_obj
        ns.vals, ns.prompt = vals, prompt
        assert bool(ns.vals) ^ bool(ns.prompt)
        self._register(ns)

    @staticmethod
    def show_help_nav_keys(win):
        """For help screens, show the navigation keys. """
        for line in Window.get_nav_keys_blurb().splitlines():
            if line:
                win.add_header(line)

    def show_help_body(self, win):
        """ Write the help page section."""
        win.add_body('Type keys to alter choice:', curses.A_UNDERLINE)

        for ns in self.options:
            # get / coerce the current value
            value = self.get_value(ns.attr)
            assert value is not None, f'cannot get value of {repr(ns.attr)}'
            choices = ns.vals if ns.vals else [value]

            win.add_body(f'{ns.descr:>{self.align}}: ')

            for choice in choices:
                shown = f'{choice}'
                if isinstance(choice, bool):
                    shown = "ON" if choice else "off"
                win.add_body(' ', resume=True)
                win.add_body(shown, resume=True,
                    attr=curses.A_REVERSE if choice == value else None)

            for comment in ns.comments:
                win.add_body(f'{"":>{self.align}}:  {comment}')

    def do_key(self, key, win):
        """Do the automated processing of a key."""
        ns = self.key_to_option.get(key, None)
        if ns is None:
            return None
        value = self.get_value(ns.attr)
        if ns.vals:
            idx = ns.vals.index(value) if value in ns.vals else -1
            value = ns.vals[(idx+1) % len(ns.vals)] # choose next
        else:
            value = win.answer(prompt=ns.prompt, seed=str(value))
        setattr(ns.obj, ns.attr, value)
        return value

class Window:
    """ Layer above curses to encapsulate what we need """
    timeout_ms = 200
    static_scr = None
    nav_keys = """
        Navigation:    H/M/L:   top/middle/end-of-page
            k, UP:  up one row               0, HOME:  first row
          j, DOWN:  down one row              $, END:  last row
           Ctrl-u:  half-page up       Ctrl-b, PPAGE:  page up
           Ctrl-d:  half-page down     Ctrl-f, NPAGE:  page down
    """
    def __init__(self, head_line=True, head_rows=50, body_rows=200,
                 body_cols=200, keys=None, pick_mode=False, pick_size=1,
                 mod_pick=None):
        self.scr = self._start_curses()

        self.head = SimpleNamespace(
            pad=curses.newpad(head_rows, body_cols),
            rows=head_rows,
            cols=body_cols,
            row_cnt=0,  # no. head rows added
            texts = [],
            view_cnt=0,  # no. head rows viewable (NOT in body)
        )
        self.body = SimpleNamespace(
            pad = curses.newpad(body_rows, body_cols),
            rows= body_rows,
            cols=body_cols,
            row_cnt = 0,
            texts = []
        )
        self.mod_pick = mod_pick # call back to modify highlighted row
        self.hor_line_cnt = 1 if head_line else 0 # no. h-lines in header
        self.scroll_pos = 0  # how far down into body are we?
        self.max_scroll_pos = 0
        self.pick_pos = 0 # in highlight mode, where are we?
        self.last_pick_pos = -1 # last highlighted position
        self.pick_mode = pick_mode # whether in highlight mode
        self.pick_size = pick_size # whether in highlight mode
        self.rows, self.cols = 0, 0
        self.body_cols, self.body_rows = body_cols, body_rows
        self.scroll_view_size = 0  # no. viewable lines of the body
        self.handled_keys = set(keys) if isinstance(keys, (set, list)) else []
        self._set_screen_dims()
        self.calc()
        
    def get_pad_width(self):
        # how much space to actually draw chars?
        return min(self.cols-1, self.body_cols)

    @staticmethod
    def get_nav_keys_blurb():
        """For a help screen, describe the nav keys"""
        return textwrap.dedent(Window.nav_keys)

    def _set_screen_dims(self):
        """Recalculate dimensions ... return True if geometry changed."""
        rows, cols = self.scr.getmaxyx()
        same = bool(rows == self.rows and cols == self.cols)
        self.rows, self.cols = rows, cols
        return same

    @staticmethod
    def _start_curses():
        """ Curses initial setup.  Note: not using curses.wrapper because we
        don't wish to change the colors. """
        atexit.register(Window.stop_curses)
        ignore_ctrl_c()
        Window.static_scr = scr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        curses.curs_set(0)
        scr.keypad(1)
        scr.timeout(Window.timeout_ms)
        scr.clear()
        return scr

    def set_pick_mode(self, on=True, pick_size=1):
        """Set whether in highlight mode."""
        was_on, was_size = self.pick_mode, self.pick_size
        self.pick_mode = bool(on)
        self.pick_size = max(pick_size, 1)
        if self.pick_mode and (not was_on or was_size != self.pick_size):
            self.last_pick_pos = -2 # indicates need to clear them all

    @staticmethod
    def stop_curses():
        """ Curses shutdown (registered to be called on exit). """
        if Window.static_scr:
            curses.nocbreak()
            curses.echo()
            Window.static_scr.keypad(0)
            curses.endwin()
            Window.static_scr = None
            restore_ctrl_c()

    def calc(self):
        """Recalculate dimensions ... return True if geometry changed."""
        same = self._set_screen_dims()
        self.head.view_cnt = min(self.rows - self.hor_line_cnt, self.head.row_cnt)
        self.scroll_view_size = self.rows - self.head.view_cnt - self.hor_line_cnt
        self.max_scroll_pos = max(self.body.row_cnt - self.scroll_view_size, 0)
        self.body_base = self.head.view_cnt + self.hor_line_cnt
        return not same

    def _put(self, ns, *args):
        """ Add text to head/body pad using its namespace. args:
            - bytes (converted to str/unicode)
            - str (unicode)
            - None (same as curses.A_NORMAL)
            - int (curses attribute)
        """
        def flush(attr=None):
            nonlocal self, is_body, row, text, seg, first
            if (is_body and self.pick_mode) or attr is None:
                attr = curses.A_NORMAL
            if seg and first:
                ns.pad.addstr(row, 0, seg[0:self.get_pad_width()], attr)
            elif seg:
                _, x = ns.pad.getyx()
                cols = self.get_pad_width() - x
                if cols > 0:
                    ns.pad.addstr(seg[0:cols], attr)
            text += seg
            seg, first, attr = '', False, None

        is_body = bool(id(ns) == id(self.body))
        if ns.row_cnt < ns.rows:
            row = max(ns.row_cnt, 0)
            text, seg, first = '', '', True
            for arg in args:
                if isinstance(arg, bytes):
                    arg = arg.decode('utf-8')
                if isinstance(arg, str):
                    seg += arg  # note: add w/o spacing
                elif arg is None or isinstance(arg, (int)):
                    # assume arg is attribute ... flushes text
                    flush(attr=arg)
            flush()
            ns.texts.append(text)  # text only history
            ns.row_cnt += 1

    def put_head(self, *args):
        """ Put a line above the line."""
        self._put(self.head, *args)

    def put_body(self, *args):
        """ Put a line below the line."""
        self._put(self.body, *args)

    def _add(self, ns, text, attr=None, resume=False):
        """ Add text to head/body pad using its namespace"""
        is_body = bool(id(ns) == id(self.body))
        if ns.row_cnt < ns.rows:
            row = max(ns.row_cnt - (1 if resume else 0), 0)
            if (is_body and self.pick_mode) or attr is None:
                attr = curses.A_NORMAL
            if resume:
                _, x = ns.pad.getyx()
                cols = self.get_pad_width() - x
                if cols > 0:
                    ns.pad.addstr(text[0:cols], attr)
                    ns.texts[row] += text
            else:
                ns.pad.addstr(row, 0, text[0:self.cols], attr)
                ns.texts.append(text)  # text only history
                ns.row_cnt += 1

    def add_header(self, text, attr=None, resume=False):
        """Add text to header"""
        self._add(self.head, text, attr, resume)

    def add_body(self, text, attr=None, resume=False):
        """ Add text to body (below header and header line)"""
        self._add(self.body, text, attr, resume)

    def draw(self, y, x, text, text_attr=None, width=None, leftpad=False, header=False):
        """Draws the given text (as utf-8 or unicode) at position (row=y,col=x)
        with optional text attributes and width.
        This is more compatible with my older, simpler Window class.
        """
        ns = self.head if header else self.body
        text_attr = text_attr if text_attr else curses.A_NORMAL
        if y < 0 or y >= ns.rows or x < 0 or x >= ns.cols:
            return # nada if out of bounds
        ns.row_cnt = max(ns.row_cnt, y+1)

        uni = text if isinstance(text, str) else text.decode('utf-8')

        if width is not None:
            width = min(width, self.get_pad_width() - x)
            if width <= 0:
                return
            padlen = width - len(uni)
            if padlen > 0:
                if leftpad:
                    uni = padlen * ' ' + uni
                else:  # rightpad
                    uni += padlen * ' '
            text = uni[:width].encode('utf-8')
        else:
            text = uni.encode('utf-8')

        try:
            while y >= len(ns.texts):
                ns.texts.append('')
            ns.texts[y] = ns.texts[y][:x].ljust(x) + uni + ns.texts[y][x+len(uni):]
            ns.pad.addstr(y, x, text, text_attr)
        except curses.error:
            # this sucks, but curses returns an error if drawing the last character
            # on the screen always.  this can happen if resizing screen even if
            # special care is taken.  So, we just ignore errors.  Anyhow, you cannot
            # get decent error handling.
            pass


    def highlight_picked(self):
        """Highlight the current pick and un-highlight the previous pick."""
        def get_text(pos):
            nonlocal self
            return self.body.texts[pos][0:self.cols] if pos < len(self.body.texts) else ''

        if not self.pick_mode:
            return
        pos0, pos1 = self.last_pick_pos, self.pick_pos
        if pos0 == -2: # special flag to clear all formatting
            for row in range(self.body.row_cnt):
                line = get_text(row).ljust(self.get_pad_width())
                self.body.pad.addstr(row, 0, get_text(row), curses.A_NORMAL)
        if pos0 != pos1:
            if 0 <= pos0 < self.body.row_cnt:
                for i in range(self.pick_size):
                    line = get_text(pos0+i).ljust(self.get_pad_width())
                    self.body.pad.addstr(pos0+i, 0, line, curses.A_NORMAL)
            if 0 <= pos1 < self.body.row_cnt:
                for i in range(self.pick_size):
                    line = get_text(pos1+i)
                    if self.mod_pick:
                        line = self.mod_pick(line)
                    line = line.ljust(self.get_pad_width())
                    self.body.pad.addstr(pos1+i, 0, line, curses.A_REVERSE)
                self.last_pick_pos = pos1

    def _scroll_indicator_row(self):
        """ Compute the absolute scroll indicator row:
        - We want the top to be only when scroll_pos==0
        - We want the bottom to be only when scroll_pos=max_scroll_pos-1
        """
        if self.max_scroll_pos <= 1:
            return self.body_base
        y2, y1 = self.scroll_view_size-1, 1
        x2, x1 = self.max_scroll_pos, 1
        x = self.scroll_pos
        pos = y1 + (y2-y1)*(x-x1)/(x2-x1)
        return min(self.body_base + int(max(pos, 0)), self.rows-1)

    def _scroll_indicator_col(self):
        """ Compute the absolute scroll indicator col:
        - We want the left to be only when scroll_pos==0
        - We want the right to be only when scroll_pos=max_scroll_pos-1
        """
        if self.pick_mode:
            return self._calc_indicator(
                self.pick_pos, 0, self.body.row_cnt-1, 0, self.cols-1)
        return self._calc_indicator(
            self.scroll_pos, 0, self.max_scroll_pos, 0, self.cols-1)

    def _calc_indicator(self, pos, pos0, pos9, ind0, ind9):
        if self.max_scroll_pos <= 0:
            return -1 # not scrollable
        if pos9 - pos0 <= 0:
            return -1 # not scrollable
        if pos <= pos0:
            return ind0
        if pos >= pos9:
            return ind9
        ind = int(round(ind0 + (ind9-ind0+1)*(pos-pos0)/(pos9-pos0+1)))
        return min(max(ind, ind0+1), ind9-1)

    def render(self):
        """Draw everything added. In a loop cuz curses is a
        piece of shit."""
        for _ in range(128):
            try:
                self.render_once()
                return
            except curses.error:
                time.sleep(0.16)
                self._set_screen_dims()
                continue
        try:
            self.render_once()
        except Exception:
            Window.stop_curses()
            print(f"""curses err:
    head.row_cnt={self.head.row_cnt}
    head.view_cnt={self.head.view_cnt}
    hor_line_cnt={self.hor_line_cnt}
    body.row_cnt={self.body.row_cnt}
    scroll_pos={self.scroll_pos}
    max_scroll_pos={self.max_scroll_pos}
    pick_pos={self.pick_pos}
    last_pick_pos={self.last_pick_pos}
    pick_mode={self.pick_mode}
    pick_size={self.pick_size}
    rows={self.rows}
    cols={self.cols}
""")
            raise


    def fix_positions(self, delta=0):
        """ Ensure the vertical positions are on the playing field """
        self.calc()
        # if self.scroll_view_size <= 0:
            # self.scr.refresh()
        if self.pick_mode:
            self.pick_pos += delta
        else:
            self.scroll_pos += delta
            self.pick_pos += delta

        indent = 0
        if self.body_base < self.rows:
            ind_pos = 0 if self.pick_mode else self._scroll_indicator_row()
            if self.pick_mode:
                self.pick_pos = max(self.pick_pos, 0)
                self.pick_pos = min(self.pick_pos, self.body.row_cnt-1)
                if self.pick_pos >= 0:
                    self.pick_pos -= (self.pick_pos % self.pick_size)
                if self.pick_pos < 0:
                    self.scroll_pos = 0
                elif self.scroll_pos > self.pick_pos:
                    # light position is below body bottom
                    self.scroll_pos = self.pick_pos
                elif self.scroll_pos < self.pick_pos - (self.scroll_view_size - self.pick_size):
                    # light position is above body top
                    self.scroll_pos = self.pick_pos - (self.scroll_view_size - self.pick_size)
                self.scroll_pos = max(self.scroll_pos, 0)
                self.scroll_pos = min(self.scroll_pos, self.max_scroll_pos)
                indent = 1
            else:
                self.scroll_pos = max(self.scroll_pos, 0)
                self.scroll_pos = min(self.scroll_pos, self.max_scroll_pos)
                self.pick_pos = self.scroll_pos + ind_pos - self.body_base
                # indent = 1 if self.body.row_cnt > self.scroll_view_size else 0
        return indent

    def render_once(self):
        """Draw everything added."""

        indent = self.fix_positions()

        if indent > 0 and self.pick_mode:
            self.scr.vline(self.body_base, 0, ' ', self.scroll_view_size)
            if self.pick_pos >= 0:
                pos = self.pick_pos - self.scroll_pos + self.body_base
                self.scr.addstr(pos, 0, '>', curses.A_REVERSE)

        if self.head.view_cnt < self.rows:
            self.scr.hline(self.head.view_cnt, 0, curses.ACS_HLINE, self.cols)
            ind_pos = self._scroll_indicator_col()
            if ind_pos >= 0:
                bot, cnt = ind_pos, 1
                if 0 < ind_pos < self.cols-1:
                    width = self.scroll_view_size/self.body.row_cnt*self.cols
                    bot = max(int(round(ind_pos-width/2)), 1)
                    top = min(int(round(ind_pos+width/2)), self.cols-1)
                    cnt = top - bot
                # self.scr.addstr(self.head.view_cnt, bot, '-'*cnt, curses.A_REVERSE)
                # self.scr.hline(self.head.view_cnt, bot, curses.ACS_HLINE, curses.A_REVERSE, cnt)
                for idx in range(bot, bot+cnt):
                    self.scr.addch(self.head.view_cnt, idx, curses.ACS_HLINE, curses.A_REVERSE)

        self.scr.refresh()

        if self.body_base < self.rows:
            if self.pick_mode:
                self.highlight_picked()
            self.body.pad.refresh(self.scroll_pos, 0,
                  self.body_base, indent, self.rows-1, self.cols-1)

        if self.rows > 0:
            last_row = min(self.head.view_cnt, self.rows)-1
            if last_row >= 0:
                self.head.pad.refresh(0, 0, 0, indent, last_row, self.cols-1)


    def answer(self, prompt='Type string [then Enter]', seed='', width=80):
        """Popup"""
        def mod_key(key):
            return  7 if key == 10 else key

        # need 3 extra cols for rectangle (so we don't draw in southeast corner)
        # and 3 rows (top/prompt/bottom)
        #      +Prompt---    -----------------+
        #      | Seed-for-answer              |
        #      +---------Press ENTER to submit+
        if self.rows < 3 or self.cols < 30:
            return seed
        width = min(width, self.cols-3) # max text width
        row0, row9 = self.rows//2 - 1, self.rows//2 + 1
        col0 = (self.cols - (width+2)) // 2
        col9 = col0 + width + 2 - 1

        self.scr.clear()
        win = curses.newwin(1, width, row0+1, col0+1) # input window
        rectangle(self.scr, row0, col0, row9, col9)
        self.scr.addstr(row0, col0+1, prompt[0:width])
        win.addstr(seed[0:width-1])
        ending = 'Press ENTER to submit'[:width]
        self.scr.addstr(row9, col0+1+width-len(ending), ending)
        self.scr.refresh()
        curses.curs_set(2)
        answer = Textbox(win).edit(mod_key).strip()
        curses.curs_set(0)
        return answer

    def alert(self, title='ALERT', message='', height=1, width=80):
        """Alert box"""
        def mod_key(key):
            return  7 if key in (10, curses.KEY_ENTER) else key

        # need 3 extra cols for rectangle (so we don't draw in southeast corner)
        # and 3 rows (top/prompt/bottom)
        #      +Prompt---    -----------------+
        #      | First line for message...    |
        #      | Last line for message.       |
        #      +-----------Press ENTER  to ack+
        if self.rows < 2+height or self.cols < 30:
            return
        width = min(width, self.cols-3) # max text width
        row0 = (self.rows+height-1)//2 - 1
        row9 = row0 + height + 1
        col0 = (self.cols - (width+2)) // 2
        col9 = col0 + width + 2 - 1

        self.scr.clear()
        for row in range(self.rows):
            self.scr.insstr(row, 0, ' '*self.cols, curses.A_REVERSE)
        pad = curses.newpad(20, 200)
        win = curses.newwin(1, 1, row9-1, col9-2) # input window
        rectangle(self.scr, row0, col0, row9, col9)
        self.scr.addstr(row0, col0+1, title[0:width], curses.A_REVERSE)
        pad.addstr(message)
        ending = 'Press ENTER to ack'[:width]
        self.scr.addstr(row9, col0+1+width-len(ending), ending)
        self.scr.refresh()
        pad.refresh(0, 0, row0+1, col0+1, row9-1, col9-1)
        Textbox(win).edit(mod_key).strip()
        return

    def clear(self):
        """Clear in prep for new screen"""
        self.scr.clear()
        self.head.pad.clear()
        self.body.pad.clear()
        self.head.texts, self.body.texts, self.last_pick_pos = [], [], -1
        self.head.row_cnt = self.body.row_cnt = 0

    def prompt(self, seconds=1.0):
        """Here is where we sleep waiting for commands or timeout"""
        ctl_b, ctl_d, ctl_f, ctl_u = 2, 4, 6, 21
        elapsed = 0.0
        while elapsed < seconds:
            key = self.scr.getch()
            if key == curses.ERR:
                elapsed += self.timeout_ms / 1000
                continue
            if key in (curses.KEY_RESIZE, ) or curses.is_term_resized(self.rows, self.cols):
                # self.scr.erase()
                self._set_screen_dims()
                # self.render()
                break

            # App keys...
            if key in self.handled_keys:
                return key # return for handling

            # Navigation Keys...
            pos = self.pick_pos if self.pick_mode else self.scroll_pos
            delta = self.pick_size if self.pick_mode else 1
            was_pos = pos
            if key in (ord('k'), curses.KEY_UP):
                pos -= delta
            elif key in (ord('j'), curses.KEY_DOWN):
                pos += delta
            elif key in (ctl_b, curses.KEY_PPAGE):
                pos -= self.scroll_view_size
            elif key in (ctl_u, ):
                pos -= self.scroll_view_size//2
            elif key in (ctl_f, curses.KEY_NPAGE):
                pos += self.scroll_view_size
            elif key in (ctl_d, ):
                pos += self.scroll_view_size//2
            elif key in (ord('0'), curses.KEY_HOME):
                pos = 0
            elif key in (ord('$'), curses.KEY_END):
                pos = self.body.row_cnt - 1
            elif key in (ord('H'), ):
                pos = self.scroll_pos
            elif key in (ord('M'), ):
                pos = self.scroll_pos + self.scroll_view_size//2
            elif key in (ord('L'), ):
                pos = self.scroll_pos + self.scroll_view_size-1

            if self.pick_mode:
                self.pick_pos = pos
            else:
                self.scroll_pos = pos
                self.pick_pos = pos

            self.fix_positions()

            if pos != was_pos:
                self.render()
            # ignore unhandled keys
        return None

def no_runner():
    """Appease sbrun"""

if __name__ == '__main__':
    def main():
        """Test program"""
        def do_key(key):
            nonlocal spin, win, opts
            value = spin.do_key(key, win)
            if key in (ord('p'), ord('s')):
                win.set_pick_mode(on=opts.pick_mode, pick_size=opts.pick_size)
            elif key == ord('n'):
                win.alert(title='Info', message=f'got: {value}')
            return value

        spin = OptionSpinner()
        spin.add_key('help_mode', '? - toggle help screen', vals=[False, True])
        spin.add_key('pick_mode', 'p - toggle pick mode', vals=[False, True])
        spin.add_key('pick_size', 's - #rows in pick', vals=[1, 2, 3])
        spin.add_key('name', 'n - select name', prompt='Provide Your Name:')
        spin.add_key('mult', 'm - row multiplier', vals=[0.5, 0.9, 1.0, 1.1, 2, 4, 16])
        opts = spin.default_obj

        win = Window(head_line=True, keys=spin.keys)
        opts.name = "[hit 'n' to enter name]"
        for loop in range(100000000000):
            body_size = int(round(win.scroll_view_size*opts.mult))
            if opts.help_mode:
                win.set_pick_mode(False)
                spin.show_help_nav_keys(win)
                spin.show_help_body(win)
            else:
                win.set_pick_mode(opts.pick_mode, opts.pick_size)
                win.add_header(f'Header: {loop} "{opts.name}"')
                for idx, line in enumerate(range(body_size//opts.pick_size)):
                    win.add_body(f'Main pick: {loop}.{line}')
                    for num in range(1, opts.pick_size):
                        win.draw(num+idx*opts.pick_size, 0, f'  addon: {loop}.{line}')
            win.render()
            _ = do_key(win.prompt(seconds=5))
            win.clear()

    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exce:
        Window.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
        if dump_str:
            print(dump_str)
