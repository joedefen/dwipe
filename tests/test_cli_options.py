"""Tests for command-line option handling (dwipe.main -> DiskWipe)."""
from types import SimpleNamespace

from dwipe.PersistentState import PersistentState


def test_cli_mode_flag_overrides_wipe_mode(tmp_path):
    """`--mode -V` (argparse dest 'mode') must reach opts.wipe_mode."""
    from dwipe.DiskWipe import DiskWipe

    persistent = PersistentState(config_path=tmp_path / "state.json")
    opts = SimpleNamespace(mode="-V")

    DiskWipe(opts=opts, persistent_state=persistent)
    assert opts.wipe_mode == "-V"


def test_wipe_mode_defaults_when_mode_absent(tmp_path):
    from dwipe.DiskWipe import DiskWipe

    persistent = PersistentState(config_path=tmp_path / "state.json")
    opts = SimpleNamespace()

    DiskWipe(opts=opts, persistent_state=persistent)
    assert opts.wipe_mode == "+V"


def test_the_theme_picker_offers_mono_after_the_library_palettes():
    from console_window import Theme
    from dwipe.DiskWipe import MONO_THEME, THEME_CHOICES

    assert THEME_CHOICES == (*Theme.list_all(), MONO_THEME)


def test_no_color_in_the_environment_keeps_the_remembered_palette():
    from dwipe.DiskWipe import resolve_theme_selection

    selected, theme, colors = resolve_theme_selection(
        saved_theme="nord", environ={"NO_COLOR": "1"})

    # The per-run switch must not rewrite the remembered palette; the picker
    # still reports mono because color is off.
    assert (selected, theme, colors) == ("nord", "nord", False)


def test_a_remembered_mono_theme_starts_with_color_off():
    from dwipe.DiskWipe import MONO_THEME, resolve_theme_selection

    selected, theme, colors = resolve_theme_selection(
        saved_theme=MONO_THEME, environ={})

    assert (selected, theme, colors) == (MONO_THEME, "", False)


def test_an_explicit_theme_is_a_request_for_color():
    from dwipe.DiskWipe import resolve_theme_selection

    selected, theme, colors = resolve_theme_selection(
        cli_theme="gruvbox", saved_theme="mono", environ={"NO_COLOR": "1"})

    assert (selected, theme, colors) == ("gruvbox", "gruvbox", True)


def test_the_no_color_switch_wins_over_an_explicit_theme():
    from dwipe.DiskWipe import resolve_theme_selection

    selected, theme, colors = resolve_theme_selection(
        cli_theme="gruvbox", no_color=True, saved_theme="nord", environ={})

    assert (selected, theme, colors) == ("gruvbox", "gruvbox", False)


def test_choosing_mono_is_remembered_and_disables_color(tmp_path, monkeypatch):
    from console_window import appearance
    from dwipe.DiskWipe import MONO_THEME, DiskWipe

    monkeypatch.setattr(appearance, "_COLORS_ENABLED", True)
    persistent = PersistentState(config_path=tmp_path / "state.json")
    app = DiskWipe(opts=SimpleNamespace(), persistent_state=persistent)

    app.apply_theme_choice(MONO_THEME)

    assert persistent.state["theme"] == MONO_THEME
    assert appearance.colors_enabled() is False


def test_the_picker_cycles_from_the_last_palette_into_mono_and_back(
        tmp_path, monkeypatch):
    from console_window import ConsoleWindow, ConsoleWindowOpts, Theme, appearance
    from console_window.backend import HeadlessBackend
    from dwipe.DiskWipe import MONO_THEME, THEME_CHOICES, DiskWipe, ThemeScreen

    monkeypatch.setattr(appearance, "_COLORS_ENABLED", True)
    monkeypatch.setattr(Theme, "_current", None)
    persistent = PersistentState(config_path=tmp_path / "state.json")
    app = DiskWipe(opts=SimpleNamespace(), persistent_state=persistent)
    last_palette = THEME_CHOICES[-2]
    win = ConsoleWindow(opts=ConsoleWindowOpts(theme=last_palette),
                        backend=HeadlessBackend(rows=24, cols=80),
                        singleton=False)
    try:
        app.win = win
        app.opts.theme = last_palette
        screen = ThemeScreen(app)

        screen.spin_theme_ACTION()
        assert app.opts.theme == MONO_THEME
        assert appearance.colors_enabled() is False
        assert persistent.state["theme"] == MONO_THEME

        screen.spin_theme_ACTION()
        assert app.opts.theme == THEME_CHOICES[0]
        assert appearance.colors_enabled() is True
        assert persistent.state["theme"] == THEME_CHOICES[0]
    finally:
        win.stop_curses()
