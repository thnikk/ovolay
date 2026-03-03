#!/usr/bin/python3 -u
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
from gi.repository import Gtk, Gdk, Adw, Gtk4LayerShell, GLib  # noqa


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
    background-color: alpha(#3584e4, 0.2);
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

.window-label {
    font-size: 24px;
}

.close-button {
    padding: 5px;
    border-radius: 24px;
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-t', '--title', action='store_true', help='show title bar')
    parser.add_argument(
        '-w', '--wrap', action='store_true', help='wrap selection at ends')
    parser.add_argument(
        '-b', '--binds', nargs='+', choices=['udlr', 'hjkl', 'wasd'],
        default=['udlr', 'hjkl', 'wasd'],
        help='keybindings to enable (default: all)')
    return parser.parse_args()


class VolumeSliderRow(Gtk.Box):
    def __init__(self, sink_input, set_volume_cb, set_mute_cb):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = sink_input.index
        self.set_volume_cb = set_volume_cb
        self.set_mute_cb = set_mute_cb
        self.is_muted = bool(sink_input.mute)
        self.is_selected_item = False

        # Compute average channel volume as integer percentage
        vol_vals = sink_input.volume.values
        initial_volume = int(sum(vol_vals) / len(vol_vals) * 100)

        # Extract app and media names from proplist
        app_name = sink_input.proplist.get(
            'application.name', 'Unknown Application')
        media_name = sink_input.proplist.get('media.name')

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
        title_label.set_text(app_name)
        title_label.set_halign(Gtk.Align.START)
        title_label.set_ellipsize(3)
        title_label.set_max_width_chars(30)
        title_label.add_css_class("title-label")
        title_box.append(title_label)

        if media_name and media_name != app_name:
            subtitle_label = Gtk.Label()
            subtitle_label.set_text(media_name)
            subtitle_label.set_halign(Gtk.Align.START)
            subtitle_label.set_ellipsize(3)
            subtitle_label.set_max_width_chars(35)
            subtitle_label.add_css_class("subtitle-label")
            title_box.append(subtitle_label)

        content_box.append(title_box)
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


class VolumeOverlay(Adw.ApplicationWindow):
    def __init__(self, args, **kwargs):
        super().__init__(**kwargs)
        self.args = args
        self.current_inputs = None
        self.selected_row_index = 0
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

        # Layer Shell configuration
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_namespace(self, "volume-overlay")

        # Close window on focus loss
        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect("leave", lambda c: self.close())
        self.add_controller(focus_controller)

        # Center the window (no edge anchoring)
        for edge in [
            Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT,
            Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM
        ]:
            Gtk4LayerShell.set_anchor(self, edge, False)

        self.set_default_size(500, 1)
        self.set_size_request(500, -1)
        self.add_css_class("overlay-window")

        # Main layout
        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=20)

        if args.title:
            header_box = Gtk.CenterBox.new()
            window_label = Gtk.Label(
                label="App Volume", css_classes=["window-label"])
            header_box.set_center_widget(window_label)
            close_button = Gtk.Button(
                label="X", css_classes=["close-button"])
            header_box.set_end_widget(close_button)
            self.main_box.append(header_box)

        self.list_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.list_box.add_css_class("boxed-list")

        self.main_box.append(self.list_box)
        self.set_content(self.main_box)

        # Key controller for keyboard navigation
        evk = Gtk.EventControllerKey()
        evk.connect("key-pressed", self.on_key_pressed)
        self.add_controller(evk)

        # Close pulse connection when the window is destroyed
        self.connect("destroy", lambda w: self.pulse.close())

        self.refresh_inputs()
        self._start_event_listener()

    def _set_volume(self, index, volume_float):
        # Look up the sink input by index and set its volume
        try:
            for si in self.pulse.sink_input_list():
                if si.index == index:
                    self.pulse.volume_set_all_chans(si, volume_float)
                    break
        except pulsectl.PulseError:
            pass

    def _set_mute(self, index, muted):
        # Look up the sink input by index and set its mute state
        try:
            for si in self.pulse.sink_input_list():
                if si.index == index:
                    self.pulse.mute(si, muted)
                    break
        except pulsectl.PulseError:
            pass

    def _start_event_listener(self):
        # Watch for sink-input events in a daemon thread
        def listen():
            try:
                with pulsectl.Pulse('ovolay-events') as pulse:
                    pulse.event_mask_set('sink_input')
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
        self.refresh_inputs()
        return GLib.SOURCE_REMOVE

    def move_selection(self, direction):
        count = self.get_row_count()
        if count == 0:
            return

        if self.args.wrap:
            self.selected_row_index = (
                self.selected_row_index + direction) % count
        else:
            self.selected_row_index = max(
                0, min(self.selected_row_index + direction, count - 1))
        self.update_selection_visuals()

    def select_by_index(self, index):
        if index < self.get_row_count():
            self.selected_row_index = index
            self.update_selection_visuals()

    def get_row_count(self):
        count = 0
        row = self.list_box.get_first_child()
        while row:
            count += 1
            row = row.get_next_sibling()
        return count

    def update_selection_visuals(self):
        index = 0
        row = self.list_box.get_first_child()
        while row:
            if hasattr(row, 'set_selected'):
                row.set_selected(index == self.selected_row_index)
            index += 1
            row = row.get_next_sibling()

    def adjust_selected_volume(self, delta):
        row = self.get_selected_row()
        if row and hasattr(row, 'adjust_volume'):
            row.adjust_volume(delta)

    def toggle_selected_mute(self):
        row = self.get_selected_row()
        if row and hasattr(row, 'toggle_mute'):
            row.toggle_mute()

    def get_selected_row(self):
        index = 0
        row = self.list_box.get_first_child()
        while row:
            if index == self.selected_row_index:
                return row
            index += 1
            row = row.get_next_sibling()
        return None

    def refresh_inputs(self):
        try:
            inputs = self.pulse.sink_input_list()
            new_indices = frozenset(si.index for si in inputs)

            if new_indices != self.current_inputs:
                self.current_inputs = new_indices

                # Clear existing rows
                child = self.list_box.get_first_child()
                while child:
                    self.list_box.remove(child)
                    child = self.list_box.get_first_child()

                if not inputs:
                    self.list_box.append(
                        Gtk.Label(label="No sink inputs"))
                    self.selected_row_index = 0
                else:
                    for si in sorted(inputs, key=lambda x: x.index):
                        self.list_box.append(VolumeSliderRow(
                            si, self._set_volume, self._set_mute))
                    self.selected_row_index = 0
                    self.update_selection_visuals()

        except pulsectl.PulseError:
            pass
        return True

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.close()
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
        elif Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            # Number keys 1-9 select items by position
            self.select_by_index(keyval - Gdk.KEY_1)
            return True
        return False


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.thnikk.VolumeOverlay")

    def do_activate(self):
        # Load CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        args = parse_args()

        self.win = VolumeOverlay(args, application=self)
        self.win.present()


if __name__ == "__main__":
    app = Application()
    app.run()
