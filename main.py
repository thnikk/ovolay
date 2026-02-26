#!/usr/bin/python3 -u
from ctypes import CDLL
import subprocess

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
    border-radius: 25px;
    border: 1px solid @borders;
    padding: 15px;
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
"""


class VolumeSliderRow(Gtk.Box):
    def __init__(self, index, app_name, media_name, initial_volume,
                 is_muted):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = index
        self.is_muted = is_muted
        self.volume = initial_volume
        self.is_selected_item = False
        self.add_css_class("volume-row")
        self.set_hexpand(True)

        # Use an overlay to put content over a progress bar background
        overlay = Gtk.Overlay()

        # Background progress bar for volume level
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(initial_volume / 100.0)
        self.progress_bar.add_css_class("volume-progress")
        overlay.set_child(self.progress_bar)

        # Content box
        content_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content_box.add_css_class("volume-row-content")

        # Create title/subtitle box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
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

        # Add scroll controller for volume adjustment
        sc = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", self.on_scroll)
        self.add_controller(sc)

        # Left click drag to set/drag volume
        self.drag_gesture = Gtk.GestureDrag.new()
        self.drag_gesture.set_button(1)
        self.drag_gesture.connect("drag-begin", self.on_drag_begin)
        self.drag_gesture.connect("drag-update", self.on_drag_update)
        self.add_controller(self.drag_gesture)

        # Right click to mute
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
        volume = int(adjustment.get_value())
        subprocess.run(["pactl", "set-sink-input-volume",
                       str(self.index), f"{volume}%"])
        self.update_ui()

    def adjust_volume(self, delta):
        current = self.adjustment.get_value()
        new = max(0, min(100, current + delta))
        self.adjustment.set_value(new)

    def toggle_mute(self):
        self.is_muted = not self.is_muted
        mute_state = "1" if self.is_muted else "0"
        subprocess.run(["pactl", "set-sink-input-mute",
                       str(self.index), mute_state])
        self.update_ui()


class VolumeOverlay(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_inputs = None  # Track current sink input indices
        self.selected_row_index = 0  # Track selected row for keyboard nav

        # Layer Shell Configuration
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_namespace(self, "volume-overlay")

        # Close window when it loses focus
        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect("leave", lambda controller: self.close())
        self.add_controller(focus_controller)

        # Center the window
        for edge in [Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT,
                     Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM]:
            Gtk4LayerShell.set_anchor(self, edge, False)

        self.set_default_size(500, 1)
        self.set_size_request(500, -1)
        self.add_css_class("overlay-window")

        # Main Layout
        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.list_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.list_box.add_css_class("boxed-list")

        self.main_box.append(self.list_box)
        self.set_content(self.main_box)

        # Key Controller
        evk = Gtk.EventControllerKey()
        evk.connect("key-pressed", self.on_key_pressed)
        self.add_controller(evk)

        self.refresh_inputs()
        GLib.timeout_add_seconds(2, self.refresh_inputs)

    def move_selection(self, direction):
        count = self.get_row_count()
        if count == 0:
            return

        # Update selected index
        self.selected_row_index = (self.selected_row_index + direction) % count
        self.update_selection_visuals()

    def select_by_index(self, index):
        count = self.get_row_count()
        # Only select if index is valid
        if index < count:
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
            output = subprocess.check_output(
                ["pactl", "list", "sink-inputs"], text=True
            )
            blocks = output.strip().split("\n\n")

            # Extract current sink input indices
            new_inputs = set()
            input_data = {}

            for block in blocks:
                if "Sink Input #" in block:
                    lines = block.splitlines()
                    idx = lines[0].split("#")[-1].strip()
                    new_inputs.add(idx)

                    app_name = "Unknown Application"
                    media_name = None
                    volume = 0
                    is_muted = False

                    for line in lines:
                        if "application.name =" in line:
                            app_name = line.split("=")[-1].strip().strip('"')
                        elif "media.name =" in line:
                            media_name = line.split("=")[-1].strip().strip('"')
                        elif "Volume:" in line and "%" in line:
                            parts = line.split("/")
                            if len(parts) > 1:
                                volume = int(parts[1].strip().replace("%", ""))
                        elif "Mute:" in line:
                            is_muted = "yes" in line.lower()

                    input_data[idx] = (app_name, media_name, volume, is_muted)

            # Only rebuild if inputs have changed
            if new_inputs != self.current_inputs:
                self.current_inputs = new_inputs

                # Remove all children from box
                child = self.list_box.get_first_child()
                while child:
                    self.list_box.remove(child)
                    child = self.list_box.get_first_child()

                if not input_data:
                    # Show placeholder when no sink inputs
                    placeholder = Gtk.Label(label="No sink inputs")
                    self.list_box.append(placeholder)
                    self.selected_row_index = 0
                else:
                    for idx in sorted(input_data.keys()):
                        app_name, media_name, volume, is_muted = \
                            input_data[idx]
                        self.list_box.append(
                            VolumeSliderRow(
                                idx, app_name, media_name, volume, is_muted))

                    # Reset selection and update visuals
                    self.selected_row_index = 0
                    self.update_selection_visuals()

        except Exception:
            pass
        return True

    def update_all_rows(self):
        self.update_selection_visuals()

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape or keyval == Gdk.KEY_q:
            self.close()
            return True
        elif keyval == Gdk.KEY_Up or keyval == Gdk.KEY_k:
            self.move_selection(-1)
            return True
        elif keyval == Gdk.KEY_Down or keyval == Gdk.KEY_j:
            self.move_selection(1)
            return True
        elif keyval == Gdk.KEY_Left or keyval == Gdk.KEY_h:
            self.adjust_selected_volume(-5)
            return True
        elif keyval == Gdk.KEY_Right or keyval == Gdk.KEY_l:
            self.adjust_selected_volume(5)
            return True
        elif keyval == Gdk.KEY_m or keyval == Gdk.KEY_space:
            self.toggle_selected_mute()
            return True
        elif keyval >= Gdk.KEY_1 and keyval <= Gdk.KEY_9:
            # Number keys 1-9 select corresponding items
            index = keyval - Gdk.KEY_1  # 1 maps to index 0, 2 to 1, etc.
            self.select_by_index(index)
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

        # Create and show the overlay
        self.win = VolumeOverlay(application=self)
        self.win.present()


if __name__ == "__main__":
    app = Application()
    app.run()
