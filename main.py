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
    background-color: @window_bg_color;
    border-radius: 24px;
    border: 1px solid @borders;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
}

.scrim {
    background-color: rgba(0, 0, 0, 0.5);
}
"""


class VolumeSliderRow(Adw.ActionRow):
    def __init__(self, index, name, initial_volume):
        super().__init__()
        self.index = index

        # Create a fixed-width box for the title
        title_box = Gtk.Box()
        title_box.set_size_request(150, -1)  # Fixed width for program names
        title_label = Gtk.Label()

        # Truncate name if too long
        display_name = name[:12] + "..." if len(name) > 12 else name
        title_label.set_text(display_name)
        title_label.set_halign(Gtk.Align.START)

        title_box.append(title_label)
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
        self.scale.connect("value-changed", self.on_volume_changed)

        scale_box.append(self.scale)
        self.add_suffix(scale_box)

    def on_volume_changed(self, scroll):
        volume = int(self.adjustment.get_value())
        subprocess.run(["pactl", "set-sink-input-volume",
                       str(self.index), f"{volume}%"])


class ScrimWindow(Gtk.Window):
    """A full-screen transparent window to dim the background."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.TOP)

        # Fill the entire screen
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.LEFT, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)

        self.add_css_class("scrim")


class VolumeOverlay(Adw.ApplicationWindow):
    def __init__(self, scrim, **kwargs):
        super().__init__(**kwargs)
        self.scrim = scrim
        self.current_inputs = set()  # Track current sink input indices

        # Layer Shell Configuration
        Gtk4LayerShell.init_for_window(self)
        # Place above the scrim
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(
            self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)

        # Center the window
        for edge in [Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT,
                     Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM]:
            Gtk4LayerShell.set_anchor(self, edge, False)

        self.set_default_size(500, -1)
        self.add_css_class("overlay-window")

        # Main Layout
        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.main_box.set_margin_top(24)
        self.main_box.set_margin_bottom(24)
        self.main_box.set_margin_start(24)
        self.main_box.set_margin_end(24)

        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("boxed-list")

        # scrolled = Gtk.ScrolledWindow()
        # scrolled.set_child(self.list_box)
        # scrolled.set_vexpand(True)
        # scrolled.set_propagate_natural_height(True)
        # scrolled.set_min_content_height(250)

        label = Gtk.Label(label="App Volume")
        label.add_css_class("title-1")

        self.main_box.append(label)
        # self.main_box.append(scrolled)
        self.main_box.append(self.list_box)
        self.set_content(self.main_box)

        # Key Controller
        evk = Gtk.EventControllerKey()
        evk.connect("key-pressed", self.on_key_pressed)
        self.add_controller(evk)

        self.refresh_inputs()
        GLib.timeout_add_seconds(2, self.refresh_inputs)

    def on_key_pressed(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.scrim.close()
            self.close()
            return True
        return False

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

                    name, volume = "Unknown Application", 0
                    for line in lines:
                        if "application.name =" in line:
                            name = line.split("=")[-1].strip().strip('"')
                        elif "Volume:" in line and "%" in line:
                            parts = line.split("/")
                            if len(parts) > 1:
                                volume = int(parts[1].strip().replace("%", ""))
                    input_data[idx] = (name, volume)

            # Only rebuild if inputs have changed
            if new_inputs != self.current_inputs:
                self.current_inputs = new_inputs
                self.list_box.remove_all()
                for idx in sorted(input_data.keys()):
                    name, volume = input_data[idx]
                    self.list_box.append(VolumeSliderRow(idx, name, volume))

        except Exception:
            pass
        return True


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.example.VolumeOverlay")

    def do_activate(self):
        # Load CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Create and show the scrim first
        self.scrim = ScrimWindow(application=self)
        self.scrim.present()

        # Create and show the overlay
        self.win = VolumeOverlay(scrim=self.scrim, application=self)
        self.win.present()


if __name__ == "__main__":
    app = Application()
    app.run()
