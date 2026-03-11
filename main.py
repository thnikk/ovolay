#!/usr/bin/python3 -u
import signal
import sys
import os
from pathlib import Path

# Signal a running daemon and exit before importing anything heavy.
# This keeps the trigger path (ovolay with no args) near-instant.
_runtime_dir = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
_pid_file = os.path.join(_runtime_dir, 'ovolay.pid')
_help_flags = {'-h', '--help'}
_replace_flags = {'-r', '--replace'}
_replacing = bool(_replace_flags.intersection(sys.argv))
if ('--daemon' not in sys.argv
        and '-d' not in sys.argv
        and not _help_flags.intersection(sys.argv)
        and not _replacing):
    try:
        with open(_pid_file) as _fh:
            _pid = int(_fh.read().strip())
        os.kill(_pid, signal.SIGUSR1)
        sys.exit(0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass

if _replacing:
    # Kill the running instance before launching the new one
    try:
        with open(_pid_file) as _fh:
            _pid = int(_fh.read().strip())
        os.kill(_pid, signal.SIGTERM)
        # Wait briefly for the process to exit and clear the PID file
        import time as _time
        for _ in range(20):
            _time.sleep(0.05)
            if not os.path.exists(_pid_file):
                break
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass
    # Remove a stale PID file if the process did not clean it up
    try:
        os.unlink(_pid_file)
    except FileNotFoundError:
        pass

from ctypes import CDLL
import threading
import argparse
import pulsectl

# Pre-load the layer shell library
try:
    CDLL('libgtk4-layer-shell.so')
except Exception:
    pass

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import (  # noqa
    Gtk, Gdk, Adw, Gtk4LayerShell, GLib, Gsk, Graphene, GdkPixbuf, Gio
)

from music import MusicTab  # noqa
from widgets import VScrollGradientBox, VolumeSliderRow  # noqa

# Load CSS from style.css next to this file
_CSS_PATH = Path(__file__).parent / 'style.css'
CSS = _CSS_PATH.read_text()


def get_pid_file() -> str:
    """Return the path to the daemon PID file."""
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
    return os.path.join(runtime_dir, 'ovolay.pid')



def _write_pid(pid_file: str) -> None:
    """Write the current PID to pid_file."""
    with open(pid_file, 'w') as fh:
        fh.write(str(os.getpid()))


def _read_pid(pid_file: str):
    """Read PID from pid_file; return None on error."""
    try:
        with open(pid_file) as fh:
            return int(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-w', '--wrap', action='store_true', help='wrap selection at ends')
    parser.add_argument(
        '-b', '--binds', nargs='+', choices=['udlr', 'hjkl', 'wasd'],
        default=['udlr', 'hjkl', 'wasd'],
        help='keybindings to enable (default: all)')
    parser.add_argument(
        '-t', '--tab',
        choices=['apps', 'outputs', 'inputs', 'music'],
        default='apps', help='tab to show on startup (default: apps)')
    parser.add_argument(
        '--screenshot', metavar='PATH',
        help=argparse.SUPPRESS)
    parser.add_argument(
        '-W', '--window', action='store_true',
        help='run as a regular window without layer shell')
    parser.add_argument(
        '-p', '--player', metavar='NAME', default=None,
        help='MPRIS2 player name to use (default: first found)')
    parser.add_argument(
        '-r', '--replace', action='store_true',
        help='replace the running instance')
    parser.add_argument(
        '-d', '--daemon', action='store_true',
        help='run as a foreground daemon; show window on SIGUSR1')
    parser.add_argument(
        '-l', '--limit-height', action='store_true',
        help='limit tab height to ~3 visible items using scroll boxes')
    parser.set_defaults(daemonized=False)
    return parser.parse_args()


def _capture_widget(widget, path):
    """
    Render a GTK widget to a PNG file with alpha transparency.
    Uses Gsk.CairoRenderer so the window background is not composited.
    """
    w = widget.get_allocated_width()
    h = widget.get_allocated_height()
    if w <= 0 or h <= 0:
        return False

    snapshot = Gtk.Snapshot()
    paintable = Gtk.WidgetPaintable.new(widget)
    paintable.snapshot(snapshot, w, h)

    node = snapshot.to_node()
    if not node:
        return False

    native = widget.get_native()
    if not native:
        return False

    renderer = Gsk.CairoRenderer()
    renderer.realize(native.get_surface())

    viewport = Graphene.Rect()
    viewport.init(0, 0, w, h)
    texture = renderer.render_texture(node, viewport)
    renderer.unrealize()

    if not texture:
        return False
    return texture.save_to_png(path)


def _vol_pct(obj):
    """Return average channel volume as integer percentage."""
    vals = obj.volume.values
    return int(sum(vals) / len(vals) * 100)


class VolumeOverlay(Adw.ApplicationWindow):
    def __init__(self, args, **kwargs):
        super().__init__(**kwargs)
        self.args = args
        self.current_tab = 'apps'
        # Per-tab selection index and known-device-index cache
        self.selected_indices = {'apps': 0, 'outputs': 0, 'inputs': 0}
        self._known = {'apps': None, 'outputs': None, 'inputs': None}
        # Cache of the current default device name per tab
        self._known_defaults = {'outputs': None, 'inputs': None}
        self._refresh_pending = False

        # PulseAudio connection for control operations (main thread only)
        self.pulse = pulsectl.Pulse('ovolay-control')

        # Navigation key sets
        self.up_keys = []
        self.down_keys = []
        self.left_keys = []
        self.right_keys = []

        if 'udlr' in self.args.binds:
            self.up_keys.append(Gdk.KEY_Up)
            self.down_keys.append(Gdk.KEY_Down)
            self.left_keys.append(Gdk.KEY_Left)
            self.right_keys.append(Gdk.KEY_Right)
        if 'hjkl' in self.args.binds:
            self.up_keys.append(Gdk.KEY_k)
            self.down_keys.append(Gdk.KEY_j)
            self.left_keys.append(Gdk.KEY_h)
            self.right_keys.append(Gdk.KEY_l)
        if 'wasd' in self.args.binds:
            self.up_keys.append(Gdk.KEY_w)
            self.down_keys.append(Gdk.KEY_s)
            self.left_keys.append(Gdk.KEY_a)
            self.right_keys.append(Gdk.KEY_d)

        if not self.args.window:
            # Layer Shell configuration
            Gtk4LayerShell.init_for_window(self)
            Gtk4LayerShell.set_keyboard_mode(
                self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
            Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
            Gtk4LayerShell.set_namespace(self, "volume-overlay")

            # Center the window (no edge anchoring)
            for edge in [
                Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT,
                Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM
            ]:
                Gtk4LayerShell.set_anchor(self, edge, False)

            # Close on focus loss only in layer shell mode.
            # Defer the dismiss by one idle tick so that tab switches
            # (which briefly move focus before re-seating it) do not
            # accidentally close the window.
            focus_controller = Gtk.EventControllerFocus()
            focus_controller.connect(
                "leave", lambda c: GLib.idle_add(self._dismiss_if_unfocused))
            self.add_controller(focus_controller)

        self.set_default_size(550, 1)
        self.set_size_request(550, -1)
        self.add_css_class("overlay-window")
        if self.args.window:
            self.add_css_class("windowed")

        # Main layout
        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=20)

        # Tab bar: ViewSwitcher selects pages in ViewStack
        self.view_stack = Adw.ViewStack()
        self.view_stack.add_css_class("tab-content")
        self.switcher = Adw.ViewSwitcher()
        self.switcher.set_stack(self.view_stack)
        self.switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        self.switcher.set_hexpand(True)

        # Close button sits to the right of the tab switcher
        close_icon = Gtk.Image.new_from_icon_name("window-close-symbolic")
        close_button = Gtk.Button(css_classes=["close-button", "circular"])
        close_button.set_child(close_icon)
        close_button.connect("clicked", lambda b: self._dismiss())
        close_button.set_margin_start(10)

        tab_row = Gtk.CenterBox()
        tab_row.set_center_widget(self.switcher)
        tab_row.set_end_widget(close_button)

        # Prevent focus on the tab bar; we handle navigation ourselves
        close_button.set_focusable(False)
        self.switcher.connect(
            'realize', self._unfocus_switcher_children)

        # Build a list box for each tab and register it in the stack.
        # 3 items visible: 50px rows + 10px spacing each = 170px
        SCROLL_HEIGHT = 170
        self.list_boxes = {}
        # ScrolledWindow per tab; populated only when --limit-height is set
        self._scroll_windows = {}
        for tab_id, tab_title, icon in [
            ('apps', 'Apps', 'application-x-executable-symbolic'),
            ('outputs', 'Outputs', 'audio-speakers-symbolic'),
            ('inputs', 'Inputs', 'audio-input-microphone-symbolic'),
        ]:
            lb = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=10)
            lb.add_css_class("boxed-list")
            self.list_boxes[tab_id] = lb
            if self.args.limit_height:
                # Wrap in a scroll box capped to ~3 items
                scroll_box = VScrollGradientBox(
                    lb, max_height=SCROLL_HEIGHT)
                self._scroll_windows[tab_id] = scroll_box._scroll
                tab_child = scroll_box
            else:
                tab_child = lb
            page = self.view_stack.add_titled(
                tab_child, tab_id, tab_title)
            page.set_icon_name(icon)

        # Music tab has its own widget rather than a generic list box
        self.music_tab = MusicTab(player_filter=self.args.player)
        music_page = self.view_stack.add_titled(
            self.music_tab, 'music', 'Music')
        music_page.set_icon_name('audio-x-generic-symbolic')

        # Track the visible tab for keyboard navigation
        self.view_stack.connect(
            "notify::visible-child-name", self.on_tab_changed)

        self.main_box.append(tab_row)
        self.main_box.append(self.view_stack)
        self.set_content(self.main_box)

        # Key controller for keyboard navigation; capture phase ensures
        # key events are handled before child widgets (e.g. ViewSwitcher)
        evk = Gtk.EventControllerKey()
        evk.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        evk.connect("key-pressed", self.on_key_pressed)
        self.add_controller(evk)

        # Close pulse connection when the window is destroyed
        self.connect("destroy", lambda w: self.pulse.close())

        # Initial data load; fetch server_info once and share it
        try:
            server_info = self.pulse.server_info()
        except pulsectl.PulseError:
            server_info = None
        self.refresh_apps()
        self.refresh_outputs(server_info)
        self.refresh_inputs_tab(server_info)
        self._start_event_listener()

        # Switch to the requested startup tab
        self.view_stack.set_visible_child_name(self.args.tab)

        # Schedule screenshot capture after first paint if requested
        if self.args.screenshot:
            GLib.idle_add(self._do_screenshot)

    def _dismiss(self):
        """Hide the window; in daemon mode keep it alive for reuse."""
        if self.args.daemonized:
            if not self.get_visible():
                return
            # Release exclusive keyboard grab so other apps keep input
            Gtk4LayerShell.set_keyboard_mode(
                self, Gtk4LayerShell.KeyboardMode.NONE)
            self.set_visible(False)
            print('window hidden')
        else:
            self.close()

    def _dismiss_if_unfocused(self):
        """Dismiss only if the window still has no focus after idle."""
        if not self.is_active():
            self._dismiss()
        return GLib.SOURCE_REMOVE

    def _do_screenshot(self):
        """Capture the window to a PNG file then close."""
        _capture_widget(self, self.args.screenshot)
        self._dismiss()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # PulseAudio control helpers
    # ------------------------------------------------------------------

    def _lookup_and_call(self, list_fn, index, callback):
        """Find object by index via list_fn() and invoke callback(obj)."""
        try:
            for obj in list_fn():
                if obj.index == index:
                    callback(obj)
                    break
        except pulsectl.PulseError:
            pass

    def _set_app_volume(self, index, volume):
        self._lookup_and_call(
            self.pulse.sink_input_list, index,
            lambda obj: self.pulse.volume_set_all_chans(obj, volume))

    def _set_app_mute(self, index, muted):
        self._lookup_and_call(
            self.pulse.sink_input_list, index,
            lambda obj: self.pulse.mute(obj, muted))

    def _set_output_volume(self, index, volume):
        self._lookup_and_call(
            self.pulse.sink_list, index,
            lambda obj: self.pulse.volume_set_all_chans(obj, volume))

    def _set_output_mute(self, index, muted):
        self._lookup_and_call(
            self.pulse.sink_list, index,
            lambda obj: self.pulse.mute(obj, muted))

    def _set_input_volume(self, index, volume):
        self._lookup_and_call(
            self.pulse.source_list, index,
            lambda obj: self.pulse.volume_set_all_chans(obj, volume))

    def _set_input_mute(self, index, muted):
        self._lookup_and_call(
            self.pulse.source_list, index,
            lambda obj: self.pulse.mute(obj, muted))

    def _set_output_default(self, index):
        self._lookup_and_call(
            self.pulse.sink_list, index,
            lambda obj: self.pulse.sink_default_set(obj))

    def _set_input_default(self, index):
        self._lookup_and_call(
            self.pulse.source_list, index,
            lambda obj: self.pulse.source_default_set(obj))

    # ------------------------------------------------------------------
    # Event listener
    # ------------------------------------------------------------------

    def _start_event_listener(self):
        # Watch for sink, source, and sink-input events in a daemon thread
        def listen():
            try:
                with pulsectl.Pulse('ovolay-events') as pulse:
                    pulse.event_mask_set(
                        'sink_input', 'sink', 'source', 'server')
                    pulse.event_callback_set(self._on_pulse_event)
                    while True:
                        try:
                            pulse.event_listen(timeout=1)
                        except pulsectl.PulseLoopStop:
                            pass
            except Exception:
                pass
        threading.Thread(target=listen, daemon=True).start()

    def _on_pulse_event(self, ev):
        # Schedule a deduplicated UI refresh on the GTK main thread
        if not self._refresh_pending:
            self._refresh_pending = True
            GLib.idle_add(self._do_refresh)
        raise pulsectl.PulseLoopStop

    def _any_row_dragging(self):
        """Return True if any VolumeSliderRow in any tab is being dragged."""
        for tab_id, lb in self.list_boxes.items():
            child = lb.get_first_child()
            while child:
                if getattr(child, 'dragging', False):
                    return True
                child = child.get_next_sibling()
        return False

    def _do_refresh(self):
        # Called on the main thread; clears the pending flag then refreshes.
        # Skip rebuild while the user is dragging to avoid destroying the
        # active gesture widget mid-drag.
        if self._any_row_dragging():
            return GLib.SOURCE_REMOVE
        self._refresh_pending = False
        # Fetch server_info once and share across both refresh calls
        try:
            server_info = self.pulse.server_info()
        except pulsectl.PulseError:
            server_info = None
        self.refresh_apps()
        self.refresh_outputs(server_info)
        self.refresh_inputs_tab(server_info)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Tab refresh methods
    # ------------------------------------------------------------------

    def _clear_list(self, lb):
        """Remove all children from a list box."""
        child = lb.get_first_child()
        while child:
            lb.remove(child)
            child = lb.get_first_child()

    def _count_rows(self, lb):
        """Return the number of children in a list box."""
        count = 0
        child = lb.get_first_child()
        while child:
            count += 1
            child = child.get_next_sibling()
        return count

    def refresh_apps(self):
        """Rebuild the Apps list if the set of sink inputs changed."""
        try:
            items = self.pulse.sink_input_list()
            # Include volume and mute in cache key so changes redraw rows
            indices = frozenset(
                (si.index, _vol_pct(si), si.mute) for si in items)
            if indices == self._known['apps']:
                return
            self._known['apps'] = indices
            lb = self.list_boxes['apps']
            self._clear_list(lb)
            if not items:
                lb.append(Gtk.Label(label="No applications"))
                self.selected_indices['apps'] = 0
                return
            for si in sorted(items, key=lambda x: x.index):
                title = si.proplist.get(
                    'application.name', 'Unknown Application')
                subtitle = si.proplist.get('media.name')
                row = VolumeSliderRow(
                    title, subtitle, si.index, _vol_pct(si),
                    bool(si.mute),
                    self._set_app_volume, self._set_app_mute,
                    scroll_to_adjust=not self.args.limit_height)
                lb.append(row)
            # Clamp to keep position when items are removed
            current = self.selected_indices['apps']
            self.selected_indices['apps'] = min(
                current, max(0, self._count_rows(lb) - 1))
            if self.current_tab == 'apps':
                self.update_selection_visuals()
        except pulsectl.PulseError:
            pass

    def refresh_outputs(self, server_info=None):
        """Rebuild the Outputs list if the set of sinks or default changed."""
        try:
            items = self.pulse.sink_list()
            if server_info is None:
                server_info = self.pulse.server_info()
            default_name = server_info.default_sink_name
            indices = frozenset(
                (s.index, _vol_pct(s), s.mute) for s in items)
            if (indices == self._known['outputs']
                    and default_name == self._known_defaults['outputs']):
                return
            self._known['outputs'] = indices
            self._known_defaults['outputs'] = default_name
            lb = self.list_boxes['outputs']
            self._clear_list(lb)
            if not items:
                lb.append(Gtk.Label(label="No outputs"))
                self.selected_indices['outputs'] = 0
                return
            for sink in sorted(items, key=lambda x: x.index):
                row = VolumeSliderRow(
                    sink.description, sink.name, sink.index,
                    _vol_pct(sink), bool(sink.mute),
                    self._set_output_volume, self._set_output_mute,
                    is_default=(sink.name == default_name),
                    set_default_cb=self._set_output_default,
                    scroll_to_adjust=not self.args.limit_height)
                lb.append(row)
            current = self.selected_indices['outputs']
            self.selected_indices['outputs'] = min(
                current, max(0, self._count_rows(lb) - 1))
            if self.current_tab == 'outputs':
                self.update_selection_visuals()
        except pulsectl.PulseError:
            pass

    def refresh_inputs_tab(self, server_info=None):
        """Rebuild the Inputs list if the set of sources or default changed."""
        try:
            # Exclude monitor sources (loopbacks mirroring outputs)
            items = [
                s for s in self.pulse.source_list()
                if not s.name.endswith('.monitor')
            ]
            if server_info is None:
                server_info = self.pulse.server_info()
            default_name = server_info.default_source_name
            indices = frozenset(
                (s.index, _vol_pct(s), s.mute) for s in items)
            if (indices == self._known['inputs']
                    and default_name == self._known_defaults['inputs']):
                return
            self._known['inputs'] = indices
            self._known_defaults['inputs'] = default_name
            lb = self.list_boxes['inputs']
            self._clear_list(lb)
            if not items:
                lb.append(Gtk.Label(label="No inputs"))
                self.selected_indices['inputs'] = 0
                return
            for source in sorted(items, key=lambda x: x.index):
                row = VolumeSliderRow(
                    source.description, source.name, source.index,
                    _vol_pct(source), bool(source.mute),
                    self._set_input_volume, self._set_input_mute,
                    is_default=(source.name == default_name),
                    set_default_cb=self._set_input_default,
                    scroll_to_adjust=not self.args.limit_height)
                lb.append(row)
            current = self.selected_indices['inputs']
            self.selected_indices['inputs'] = min(
                current, max(0, self._count_rows(lb) - 1))
            if self.current_tab == 'inputs':
                self.update_selection_visuals()
        except pulsectl.PulseError:
            pass

    # ------------------------------------------------------------------
    # Tab switching
    # ------------------------------------------------------------------

    @staticmethod
    def _walk_widgets(widget, callback):
        """Recursively apply callback to widget and all descendants."""
        callback(widget)
        child = widget.get_first_child()
        while child:
            VolumeOverlay._walk_widgets(child, callback)
            child = child.get_next_sibling()

    def _unfocus_switcher_children(self, switcher):
        """Mark all tab switcher descendants as non-focusable."""
        self._walk_widgets(
            switcher, lambda w: w.set_focusable(False))

    def on_tab_changed(self, stack, param):
        """Update current tab and refresh selection visuals."""
        name = stack.get_visible_child_name()
        if name:
            self.current_tab = name
            self.update_selection_visuals()

    # ------------------------------------------------------------------
    # Navigation helpers (operate on the current visible tab)
    # ------------------------------------------------------------------

    def get_row_count(self):
        # Music tab has no selectable rows
        if self.current_tab == 'music':
            return 0
        lb = self.list_boxes[self.current_tab]
        count = 0
        row = lb.get_first_child()
        while row:
            count += 1
            row = row.get_next_sibling()
        return count

    def update_selection_visuals(self):
        if self.current_tab == 'music':
            return
        lb = self.list_boxes[self.current_tab]
        selected_idx = self.selected_indices[self.current_tab]
        index = 0
        row = lb.get_first_child()
        while row:
            if hasattr(row, 'set_selected'):
                row.set_selected(index == selected_idx)
            index += 1
            row = row.get_next_sibling()

    def move_selection(self, direction):
        count = self.get_row_count()
        if count == 0:
            return
        idx = self.selected_indices[self.current_tab]
        if self.args.wrap:
            idx = (idx + direction) % count
        else:
            idx = max(0, min(idx + direction, count - 1))
        self.selected_indices[self.current_tab] = idx
        self.update_selection_visuals()
        self._scroll_to_selected()

    def _scroll_to_selected(self):
        """Scroll the active tab's list so the selected row is visible.

        Keeps one row of context above/below the selection so the
        neighbour is always visible (unless at the list boundary).
        """
        sw = self._scroll_windows.get(self.current_tab)
        if sw is None:
            return
        idx = self.selected_indices[self.current_tab]
        # Row height from CSS min-height + list box spacing
        row_h = 50
        spacing = 10
        stride = row_h + spacing
        row_top = idx * stride
        row_bot = row_top + row_h
        adj = sw.get_vadjustment()
        page = adj.get_page_size()
        val = adj.get_value()
        # Reveal one extra row above when scrolling up
        if row_top - stride < val:
            adj.set_value(max(0.0, row_top - stride))
        # Reveal one extra row below when scrolling down
        elif row_bot + stride > val + page:
            adj.set_value(row_bot + stride - page)

    def select_by_index(self, index):
        if index < self.get_row_count():
            self.selected_indices[self.current_tab] = index
            self.update_selection_visuals()

    def get_selected_row(self):
        if self.current_tab == 'music':
            return None
        lb = self.list_boxes[self.current_tab]
        idx = self.selected_indices[self.current_tab]
        i = 0
        row = lb.get_first_child()
        while row:
            if i == idx:
                return row
            i += 1
            row = row.get_next_sibling()
        return None

    def adjust_selected_volume(self, delta):
        row = self.get_selected_row()
        if row and hasattr(row, 'adjust_volume'):
            row.adjust_volume(delta)

    def toggle_selected_mute(self):
        row = self.get_selected_row()
        if row and hasattr(row, 'toggle_mute'):
            row.toggle_mute()

    def set_selected_as_default(self):
        """Set the selected row as the default device (outputs/inputs only)."""
        if self.current_tab not in ('outputs', 'inputs'):
            return
        row = self.get_selected_row()
        if not (row and getattr(row, 'set_default_cb', None)):
            return
        row.set_default_cb(row.index)
        # Update indicator immediately without waiting for a PA event
        lb = self.list_boxes[self.current_tab]
        child = lb.get_first_child()
        while child:
            if hasattr(child, 'set_is_default'):
                child.set_is_default(child is row)
            child = child.get_next_sibling()
        # Invalidate the default cache so the next refresh picks up the change
        self._known_defaults[self.current_tab] = None

    # Tab order used for cycling and direct selection
    TAB_ORDER = ['apps', 'outputs', 'inputs', 'music']

    def switch_tab(self, direction):
        """Cycle to the next or previous tab by direction (+1/-1)."""
        idx = self.TAB_ORDER.index(self.current_tab)
        idx = (idx + direction) % len(self.TAB_ORDER)
        self.view_stack.set_visible_child_name(self.TAB_ORDER[idx])

    def on_key_pressed(self, controller, keyval, keycode, state):
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self._dismiss()
            return True

        # Tab / Shift+Tab cycle through tabs
        if keyval == Gdk.KEY_Tab:
            self.switch_tab(-1 if shift else 1)
            return True
        if keyval == Gdk.KEY_ISO_Left_Tab:
            # Shift+Tab often arrives as ISO_Left_Tab
            self.switch_tab(-1)
            return True

        # 1-4 switch directly to a specific tab
        if Gdk.KEY_1 <= keyval <= Gdk.KEY_4:
            tab_idx = keyval - Gdk.KEY_1
            if tab_idx < len(self.TAB_ORDER):
                self.view_stack.set_visible_child_name(
                    self.TAB_ORDER[tab_idx])
            return True

        if self.current_tab == 'music':
            # Music tab: left/right skip, up/down adjust volume
            if keyval in self.left_keys:
                self.music_tab.cmd_prev()
                return True
            elif keyval in self.right_keys:
                self.music_tab.cmd_next()
                return True
            elif keyval in self.up_keys:
                self.music_tab.adjust_volume(0.05)
                return True
            elif keyval in self.down_keys:
                self.music_tab.adjust_volume(-0.05)
                return True
            elif keyval == Gdk.KEY_space:
                self.music_tab._cmd_play_pause()
                return True
            return False

        if keyval in self.up_keys:
            self.move_selection(-1)
            return True
        elif keyval in self.down_keys:
            self.move_selection(1)
            return True
        elif keyval in self.left_keys:
            self.adjust_selected_volume(-5)
            return True
        elif keyval in self.right_keys:
            self.adjust_selected_volume(5)
            return True
        elif keyval in (Gdk.KEY_m, Gdk.KEY_space):
            self.toggle_selected_mute()
            return True
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.set_selected_as_default()
            return True
        return False


class Application(Adw.Application):
    def __init__(self, args):
        super().__init__(application_id="com.thnikk.ovolay")
        self.args = args
        self.win = None

    def _load_css(self):
        """Register application CSS with the current display."""
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _show_window(self):
        """Show the overlay, creating it on the first call."""
        if self.win is None:
            self.win = VolumeOverlay(self.args, application=self)
        if self.args.daemonized:
            # Restore exclusive keyboard grab before making it visible
            Gtk4LayerShell.set_keyboard_mode(
                self.win, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
            self.win.set_visible(True)
            print('window shown')
        self.win.present()

    def do_activate(self):
        self._load_css()
        if self.args.daemonized:
            # Pre-create and realize the window while hidden so the
            # first signal can show it with minimal delay
            self.hold()
            self.win = VolumeOverlay(self.args, application=self)
            # Present immediately so the compositor fully realizes the
            # surface, then hide on the next idle tick before the user
            # sees anything; subsequent shows are near-instant
            self.win.present()
            GLib.idle_add(lambda: self.win.set_visible(False) or False)
            GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT,
                signal.SIGUSR1,
                self._on_show_signal,
            )
            # SIGHUP recreates the window; useful after suspend/resume
            # or monitor reconnection where the surface may be stale
            GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT,
                signal.SIGHUP,
                self._on_reset_signal,
            )
        else:
            self._show_window()

    def _on_show_signal(self):
        """Open the overlay in response to SIGUSR1."""
        self._show_window()
        return GLib.SOURCE_CONTINUE

    def _on_reset_signal(self):
        """Recreate the window in response to SIGHUP."""
        if self.win is not None:
            self.win.destroy()
            self.win = None
        self._show_window()
        # Re-hide immediately; signal is for recovery, not showing
        GLib.idle_add(lambda: self.win.set_visible(False) or False)
        return GLib.SOURCE_CONTINUE


if __name__ == "__main__":
    args = parse_args()
    pid_file = get_pid_file()

    if args.daemon:
        # Run daemon in the foreground; set daemonized so the
        # Application activates signal handling and hide/show logic
        args.daemonized = True
        print(f'ovolay daemon started (pid {os.getpid()})')
        _write_pid(pid_file)
        try:
            app = Application(args)
            app.run()
        finally:
            try:
                os.unlink(pid_file)
            except FileNotFoundError:
                pass
    else:
        # Normal launch; stale PID file already cleared at top of file
        app = Application(args)
        app.run()
