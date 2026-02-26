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
    border-radius: 30px;
    border: 1px solid @borders;
    padding: 20px;
}

.volume-row {
}

.title-1 {
    font-size: 24px;
    font-weight: normal;
}

.boxed-list {
    min-height: 0;
    border-radius: 10px;
}

.boxed-list row:selected {
    outline: none;
}

.boxed-list row:focus {
    outline: none;
}

.title-label {
    font-size: 16px;
}

.subtitle-label {
    font-size: 12px;
    opacity: 0.6;
}

.close-btn {
    padding: 5px 5px;
    border-radius: 50px;
}
.close-btn:selected {outline: none;}

.mute-btn {
    border-radius: 50%;
}

.mute-btn.muted {
    background-color: alpha(red, 0.3);
}

scale.muted trough {
    opacity: 0.3;
}

scale.muted highlight {
    opacity: 0.3;
}
"""


class VolumeSliderRow(Adw.ActionRow):
    def __init__(self, index, app_name, media_name, initial_volume,
                 is_muted):
        super().__init__()
        self.index = index
        self.is_muted = is_muted
        self.add_css_class("volume-row")

        # Create title/subtitle box with fixed width
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_box.set_size_request(250, -1)
        title_box.set_hexpand(False)

        title_label = Gtk.Label()
        title_label.set_text(app_name)
        title_label.set_halign(Gtk.Align.START)
        title_label.set_valign(Gtk.Align.FILL)
        title_label.set_vexpand(True)
        title_label.set_ellipsize(3)  # ELLIPSIZE_END
        title_label.set_max_width_chars(30)
        title_label.add_css_class("title-label")
        title_box.append(title_label)

        # Add subtitle if present and different
        if media_name and media_name != app_name:
            subtitle_label = Gtk.Label()
            subtitle_label.set_text(media_name)
            subtitle_label.set_halign(Gtk.Align.START)
            subtitle_label.set_valign(Gtk.Align.START)
            subtitle_label.set_vexpand(True)
            subtitle_label.set_ellipsize(3)  # ELLIPSIZE_END
            subtitle_label.set_max_width_chars(35)
            subtitle_label.add_css_class("subtitle-label")
            title_box.append(subtitle_label)

        self.add_prefix(title_box)

        # Create a fixed-width box for the scale
        scale_box = Gtk.Box()
        scale_box.set_size_request(200, -1)  # Fixed width of 200px

        self.adjustment = Gtk.Adjustment(
            value=initial_volume, lower=0, upper=100,
            step_increment=1, page_increment=10
        )
        self.scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adjustment
        )
        self.scale.set_hexpand(True)
        self.scale.set_draw_value(False)
        self.scale.set_focusable(False)
        self.scale.connect("value-changed", self.on_volume_changed)

        scale_box.append(self.scale)
        self.add_suffix(scale_box)

        # Add mute button
        self.mute_button = Gtk.Button()
        self.mute_button.set_focusable(False)
        self.mute_button.set_valign(Gtk.Align.CENTER)
        self.mute_button.set_vexpand(False)
        self.mute_button.add_css_class("mute-btn")
        self.update_mute_icon()
        self.mute_button.connect("clicked", self.on_mute_clicked)
        self.add_suffix(self.mute_button)

    def on_volume_changed(self, scroll):
        volume = int(self.adjustment.get_value())
        subprocess.run(["pactl", "set-sink-input-volume",
                       str(self.index), f"{volume}%"])

    def adjust_volume(self, delta):
        current = int(self.adjustment.get_value())
        new = max(0, min(100, current + delta))
        self.adjustment.set_value(new)

    def on_mute_clicked(self, button):
        self.toggle_mute()

    def toggle_mute(self):
        self.is_muted = not self.is_muted
        mute_state = "1" if self.is_muted else "0"
        subprocess.run(["pactl", "set-sink-input-mute",
                       str(self.index), mute_state])
        self.update_mute_icon()

    def update_mute_icon(self):
        icon = "audio-volume-muted" if self.is_muted else "audio-volume-high"
        self.mute_button.set_icon_name(icon)

        # Update button and scale appearance
        if self.is_muted:
            self.mute_button.add_css_class("muted")
            self.scale.add_css_class("muted")
        else:
            self.mute_button.remove_css_class("muted")
            self.scale.remove_css_class("muted")


class VolumeOverlay(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.current_inputs = None  # Track current sink input indices
        self.selected_row_index = 0  # Track selected row for keyboard nav

        # Layer Shell Configuration
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
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
            orientation=Gtk.Orientation.VERTICAL, spacing=20)

        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("boxed-list")

        title_box = Gtk.CenterBox()
        label = Gtk.Label(label="App Volume")
        label.add_css_class("title-1")
        title_box.set_center_widget(label)

        close_btn = Gtk.Button.new_from_icon_name("window-close")
        close_btn.set_focusable(False)
        close_btn.add_css_class("close-btn")
        close_btn.connect("clicked", self.on_button_clicked)
        title_box.set_end_widget(close_btn)

        # self.main_box.append(label)
        self.main_box.append(title_box)
        self.main_box.append(self.list_box)
        self.set_content(self.main_box)

        # Key Controller
        evk = Gtk.EventControllerKey()
        evk.connect("key-pressed", self.on_key_pressed)
        self.add_controller(evk)

        self.refresh_inputs()
        GLib.timeout_add_seconds(2, self.refresh_inputs)

    def on_button_clicked(self, button):
        self.close()
        return

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
        elif keyval == Gdk.KEY_m:
            self.toggle_selected_mute()
            return True
        elif keyval >= Gdk.KEY_1 and keyval <= Gdk.KEY_9:
            # Number keys 1-9 select corresponding items
            index = keyval - Gdk.KEY_1  # 1 maps to index 0, 2 to 1, etc.
            self.select_by_index(index)
            return True
        return False

    def move_selection(self, direction):
        rows = self.list_box.get_first_child()
        if not rows:
            return

        # Count total rows
        count = 0
        row = rows
        while row:
            count += 1
            row = row.get_next_sibling()

        # Update selected index
        self.selected_row_index = (self.selected_row_index + direction) % count

        # Select the row
        self.list_box.select_row(
            self.list_box.get_row_at_index(self.selected_row_index))

    def select_by_index(self, index):
        rows = self.list_box.get_first_child()
        if not rows:
            return

        # Count total rows
        count = 0
        row = rows
        while row:
            count += 1
            row = row.get_next_sibling()

        # Only select if index is valid
        if index < count:
            self.selected_row_index = index
            self.list_box.select_row(
                self.list_box.get_row_at_index(self.selected_row_index))

    def adjust_selected_volume(self, delta):
        selected_row = self.list_box.get_selected_row()
        if selected_row and hasattr(selected_row, 'adjust_volume'):
            selected_row.adjust_volume(delta)

    def toggle_selected_mute(self):
        selected_row = self.list_box.get_selected_row()
        if selected_row and hasattr(selected_row, 'toggle_mute'):
            selected_row.toggle_mute()

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
                self.list_box.remove_all()

                if not input_data:
                    # Show placeholder when no sink inputs
                    placeholder = Adw.ActionRow()
                    placeholder.set_title("No sink inputs")
                    self.list_box.append(placeholder)
                    self.selected_row_index = 0
                else:
                    for idx in sorted(input_data.keys()):
                        app_name, media_name, volume, is_muted = input_data[idx]
                        self.list_box.append(
                            VolumeSliderRow(idx, app_name, media_name,
                                            volume, is_muted))

                    # Reset selection and select first row if available
                    self.selected_row_index = 0
                    if self.list_box.get_first_child():
                        self.list_box.select_row(
                            self.list_box.get_row_at_index(0))

        except Exception:
            pass
        return True


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
