#!/usr/bin/python3 -u
import signal
import sys
import os

# Signal a running daemon and exit before importing anything heavy.
# This keeps the trigger path (ovolay with no args) near-instant.
_runtime_dir = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
_pid_file = os.path.join(_runtime_dir, 'ovolay.pid')
if '--daemon' not in sys.argv and '--_daemonized' not in sys.argv:
    try:
        with open(_pid_file) as _fh:
            _pid = int(_fh.read().strip())
        os.kill(_pid, signal.SIGUSR1)
        sys.exit(0)
    except (FileNotFoundError, ValueError, ProcessLookupError):
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
from gi.repository import (  # noqa
    Gtk, Gdk, Adw, Gtk4LayerShell, GLib, Gsk, Graphene
)


# Custom CSS for rounded corners and the dimming effect
CSS = """
.overlay-window {
    font-family: Nunito;
    background-color: alpha(#1c1f26, 0.9);
    color: #d8dee9;
    border-radius: 30px;
    border: 1px solid @borders;
    padding: 20px;
}

.volume-row {
    background-color: transparent;
    border-radius: 10px;
    padding: 0;
}

.volume-progress trough {
    background-color: alpha(#ffffff, 0.1);
    border: none;
    min-height: 50px;
    border-radius: 10px;
    transition: background-color 0.15s;
}

.volume-row:selected .volume-progress trough,
.volume-row.selected .volume-progress trough {
    background-color: alpha(#ffffff, 0.2);
}

.volume-progress progress {
    background-color: color-mix(in srgb, @accent_bg_color 20%, transparent);
    border-radius: 10px 0 0 10px;
    border: none;
    min-height: 50px;
}

.volume-progress.muted progress {
    background-color: alpha(red, 0.1);
}

.volume-row-content {
    padding-left: 10px;
}

.boxed-list {
    background-color: transparent;
    min-height: 0;
}

.title-label {
    font-size: 16px;
    font-weight: 600;
}

.subtitle-label {
    font-size: 12px;
    opacity: 0.6;
}

.close-button {
/*    padding: 5px; */
    border-radius: 24px;
    background: transparent;
    box-shadow: none;
    border: none;
}

.close-button:hover {
    background: alpha(currentColor, 0.1);
}

viewswitcher {
    background-color: transparent;
}

.tab-content {
    background-color: transparent;
}

.default-icon {
    opacity: 0.6;
    padding-right: 10px;
}

.windowed {
    border-radius: 0;
    border: none;
}
"""


def get_pid_file() -> str:
    """Return the path to the daemon PID file."""
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR', '/tmp')
    return os.path.join(runtime_dir, 'ovolay.pid')


def _spawn_daemon(pid_file: str) -> None:
    """Launch a detached daemon child via Popen (no fork)."""
    import subprocess
    # Pass through all argv except --daemon; add --_daemonized so
    # the child knows it is already detached and skips re-spawning.
    child_argv = [
        a for a in sys.argv if a != '--daemon'
    ] + ['--_daemonized']
    subprocess.Popen(
        child_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


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
        '-t', '--tab', choices=['apps', 'outputs', 'inputs'],
        default='apps', help='tab to show on startup (default: apps)')
    parser.add_argument(
        '--screenshot', metavar='PATH',
        help=argparse.SUPPRESS)
    parser.add_argument(
        '--window', action='store_true',
        help='run as a regular window without layer shell')
    parser.add_argument(
        '--daemon', action='store_true',
        help='run as a background daemon; show window on SIGUSR1')
    # Internal flag set by _spawn_daemon; not intended for direct use
    parser.add_argument(
        '--_daemonized', dest='daemonized', action='store_true',
        help=argparse.SUPPRESS)
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


class VolumeSliderRow(Gtk.Box):
    def __init__(self, title, subtitle, index, initial_volume,
                 is_muted, set_volume_cb, set_mute_cb,
                 is_default=False, set_default_cb=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = index
        self.set_volume_cb = set_volume_cb
        self.set_mute_cb = set_mute_cb
        self.set_default_cb = set_default_cb
        self.is_muted = bool(is_muted)
        self.is_selected_item = False

        self.add_css_class("volume-row")
        self.set_hexpand(True)

        # Overlay puts content over the progress bar background
        overlay = Gtk.Overlay()

        # Background progress bar showing volume level
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(initial_volume / 100.0)
        self.progress_bar.add_css_class("volume-progress")
        overlay.set_child(self.progress_bar)

        # Horizontal content box
        content_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        content_box.add_css_class("volume-row-content")

        # Vertical box for title and optional subtitle
        title_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_box.set_hexpand(True)
        title_box.set_valign(Gtk.Align.CENTER)

        title_label = Gtk.Label()
        title_label.set_text(title)
        title_label.set_halign(Gtk.Align.START)
        title_label.set_ellipsize(3)
        title_label.set_max_width_chars(30)
        title_label.add_css_class("title-label")
        title_box.append(title_label)

        if subtitle and subtitle != title:
            subtitle_label = Gtk.Label()
            subtitle_label.set_text(subtitle)
            subtitle_label.set_halign(Gtk.Align.START)
            subtitle_label.set_ellipsize(3)
            subtitle_label.set_max_width_chars(35)
            subtitle_label.add_css_class("subtitle-label")
            title_box.append(subtitle_label)

        content_box.append(title_box)

        # Default device indicator; hidden when not the default
        self.default_icon = Gtk.Image.new_from_icon_name(
            "object-select-symbolic")
        self.default_icon.set_valign(Gtk.Align.CENTER)
        self.default_icon.add_css_class("default-icon")
        self.default_icon.set_visible(is_default)
        content_box.append(self.default_icon)

        overlay.add_overlay(content_box)
        self.append(overlay)

        self.adjustment = Gtk.Adjustment(
            value=initial_volume, lower=0, upper=100,
            step_increment=1, page_increment=10
        )
        self.adjustment.connect("value-changed", self.on_volume_changed)

        # Scroll controller for volume adjustment
        sc = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", self.on_scroll)
        self.add_controller(sc)

        # Left-click drag to set volume
        self.drag_gesture = Gtk.GestureDrag.new()
        self.drag_gesture.set_button(1)
        self.drag_gesture.connect("drag-begin", self.on_drag_begin)
        self.drag_gesture.connect("drag-update", self.on_drag_update)
        self.add_controller(self.drag_gesture)

        # Right-click to toggle mute
        self.right_click_gesture = Gtk.GestureClick.new()
        self.right_click_gesture.set_button(3)
        self.right_click_gesture.connect("pressed", self.on_right_click)
        self.add_controller(self.right_click_gesture)

        self.update_ui()

    def update_volume_from_x(self, x):
        width = self.get_width()
        if width > 0:
            volume = max(0, min(100, (x / width) * 100))
            self.adjustment.set_value(volume)

    def on_drag_begin(self, gesture, start_x, start_y):
        self.update_volume_from_x(start_x)

    def on_drag_update(self, gesture, offset_x, offset_y):
        success, start_x, start_y = gesture.get_start_point()
        if success:
            self.update_volume_from_x(start_x + offset_x)

    def on_right_click(self, gesture, n_press, x, y):
        self.toggle_mute()

    def on_scroll(self, controller, dx, dy):
        self.adjust_volume(-dy * 2)
        return True

    def set_selected(self, selected):
        self.is_selected_item = selected
        self.update_ui()

    def update_ui(self):
        volume_percent = self.adjustment.get_value()
        self.progress_bar.set_fraction(volume_percent / 100.0)

        if self.is_selected_item:
            self.add_css_class("selected")
        else:
            self.remove_css_class("selected")

        if self.is_muted:
            self.progress_bar.add_css_class("muted")
        else:
            self.progress_bar.remove_css_class("muted")

    def on_volume_changed(self, adjustment):
        # Forward new volume (0.0-1.0) to PulseAudio via callback
        self.set_volume_cb(self.index, adjustment.get_value() / 100.0)
        self.update_ui()

    def adjust_volume(self, delta):
        current = self.adjustment.get_value()
        self.adjustment.set_value(max(0, min(100, current + delta)))

    def toggle_mute(self):
        self.is_muted = not self.is_muted
        self.set_mute_cb(self.index, self.is_muted)
        self.update_ui()

    def set_is_default(self, value):
        """Show or hide the default device indicator."""
        self.default_icon.set_visible(bool(value))


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

            # Close on focus loss only in layer shell mode
            focus_controller = Gtk.EventControllerFocus()
            focus_controller.connect("leave", lambda c: self._dismiss())
            self.add_controller(focus_controller)

        self.set_default_size(500, 1)
        self.set_size_request(500, -1)
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

        # Build a list box for each tab and register it in the stack
        self.list_boxes = {}
        for tab_id, tab_title, icon in [
            ('apps', 'Apps', 'application-x-executable-symbolic'),
            ('outputs', 'Outputs', 'audio-speakers-symbolic'),
            ('inputs', 'Inputs', 'audio-input-microphone-symbolic'),
        ]:
            lb = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=10)
            lb.add_css_class("boxed-list")
            self.list_boxes[tab_id] = lb
            page = self.view_stack.add_titled(lb, tab_id, tab_title)
            page.set_icon_name(icon)

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
            # Release exclusive keyboard grab so other apps keep input
            Gtk4LayerShell.set_keyboard_mode(
                self, Gtk4LayerShell.KeyboardMode.NONE)
            self.set_visible(False)
        else:
            self.close()

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

    def _do_refresh(self):
        # Called on the main thread; clears the pending flag then refreshes
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
                    self._set_app_volume, self._set_app_mute)
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
                    set_default_cb=self._set_output_default)
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
                    set_default_cb=self._set_input_default)
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
        lb = self.list_boxes[self.current_tab]
        count = 0
        row = lb.get_first_child()
        while row:
            count += 1
            row = row.get_next_sibling()
        return count

    def update_selection_visuals(self):
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

    def select_by_index(self, index):
        if index < self.get_row_count():
            self.selected_indices[self.current_tab] = index
            self.update_selection_visuals()

    def get_selected_row(self):
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
    TAB_ORDER = ['apps', 'outputs', 'inputs']

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

        # 1-3 switch directly to a specific tab
        if Gdk.KEY_1 <= keyval <= Gdk.KEY_3:
            tab_idx = keyval - Gdk.KEY_1
            if tab_idx < len(self.TAB_ORDER):
                self.view_stack.set_visible_child_name(
                    self.TAB_ORDER[tab_idx])
            return True

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
        else:
            self._show_window()

    def _on_show_signal(self):
        """Open the overlay in response to SIGUSR1."""
        self._show_window()
        return GLib.SOURCE_CONTINUE


if __name__ == "__main__":
    args = parse_args()
    pid_file = get_pid_file()

    if args.daemonized:
        # Running as the detached child; write PID and start the loop
        _write_pid(pid_file)
        try:
            app = Application(args)
            app.run()
        finally:
            try:
                os.unlink(pid_file)
            except FileNotFoundError:
                pass
    elif args.daemon:
        # Parent: guard against a duplicate daemon, then spawn child
        pid = _read_pid(pid_file)
        if pid is not None:
            try:
                os.kill(pid, 0)
                print(
                    'ovolay daemon is already running',
                    file=sys.stderr,
                )
                sys.exit(1)
            except ProcessLookupError:
                # Stale PID file left by a crashed daemon
                os.unlink(pid_file)
        _spawn_daemon(pid_file)
    else:
        # Normal launch; stale PID file already cleared at top of file
        app = Application(args)
        app.run()
