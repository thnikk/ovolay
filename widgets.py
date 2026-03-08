#!/usr/bin/python3
# Widget classes for ovolay: scroll gradient boxes and volume slider rows.
import math

import cairo

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib  # noqa


def _parse_color(color):
    """Convert color to (r, g, b) float tuple.

    Accepts a float tuple, byte tuple (0-255), or hex string.
    """
    if isinstance(color, str):
        h = color.lstrip('#')
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    if any(v > 1.0 for v in color):
        return tuple(v / 255.0 for v in color)
    return tuple(color)


def _suppress_overshoot(scrolled_window):
    """Hide the built-in overshoot highlight on a ScrolledWindow."""
    scrolled_window.add_css_class("scroll-no-overshoot")


class _ScrollGradientBase(Gtk.Overlay):
    """Shared base for scroll gradient overlay boxes."""

    # #1c1f26 as floats matching .overlay-window background
    GRADIENT_SIZE = 30
    BG = (0.11, 0.122, 0.149)
    FLASH = (0.3, 0.36, 0.47)

    def __init__(
            self, child, gradient_size=None,
            bg_color=None, flash_color=None):
        super().__init__()
        self._gradient_size = (
            gradient_size if gradient_size is not None
            else self.GRADIENT_SIZE
        )
        self._bg_color = (
            _parse_color(bg_color) if bg_color else self.BG
        )
        self._flash_color = (
            _parse_color(flash_color) if flash_color else self.FLASH
        )
        self._flash_opacity = 0.0
        self._flash_dir = 0
        self._anim_id = None
        self.set_overflow(Gtk.Overflow.HIDDEN)

        self._scroll = self._make_scroll()
        self._scroll.set_child(child)
        self.set_child(self._scroll)

        self._canvas = Gtk.DrawingArea()
        self._canvas.set_can_target(False)
        self._canvas.set_draw_func(self._draw)
        self.add_overlay(self._canvas)

        adj = self._get_adjustment()
        adj.connect(
            "value-changed", lambda *_: self._canvas.queue_draw())
        adj.connect(
            "changed", lambda *_: self._canvas.queue_draw())

        self._scroll_controller = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES)
        self._scroll_controller.connect(
            "scroll", self._on_scroll_event)
        self._scroll.add_controller(self._scroll_controller)

    def _on_scroll_event(self, _controller, dx, dy):
        # Use dy for vertical boxes, dx for horizontal
        delta = dy if hasattr(self, '_sw_height') else dx
        if delta == 0:
            return False
        adj = self._get_adjustment()
        val = adj.get_value()
        max_val = adj.get_upper() - adj.get_page_size()
        if max_val <= 0:
            return False
        if delta < 0 and val <= 0:
            self._start_flash(-1)
        elif delta > 0 and val >= max_val - 0.1:
            self._start_flash(1)
        return False

    def _make_scroll(self):
        raise NotImplementedError

    def _get_adjustment(self):
        raise NotImplementedError

    def _start_flash(self, direction):
        """Animate a brief edge-flash to signal an overscroll attempt."""
        if self._anim_id:
            GLib.source_remove(self._anim_id)
        self._flash_opacity = 0.7
        self._flash_dir = direction

        def _fade():
            self._flash_opacity -= 0.05
            if self._flash_opacity <= 0.0:
                self._flash_opacity = 0.0
                self._anim_id = None
                self._canvas.queue_draw()
                return False
            self._canvas.queue_draw()
            return True

        self._anim_id = GLib.timeout_add(16, _fade)

    def _rounded_rect(self, cr, x, y, w, h, r):
        """Trace a rounded rectangle path."""
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 2 * math.pi)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.close_path()

    def _draw(self, _area, cr, width, height, *_args):
        raise NotImplementedError


class VScrollGradientBox(_ScrollGradientBase):
    """Wrap a child in a vertical ScrolledWindow with gradient edges.

    Provides edge-fade gradients and an overscroll flash effect.
    """

    def __init__(
            self, child, height=0, max_height=None, width=0,
            gradient_size=None, bg_color=None, flash_color=None):
        # Store before super().__init__() calls _make_scroll()
        self._sw_height = height
        self._max_height = max_height
        self._sw_width = width
        super().__init__(
            child, gradient_size=gradient_size,
            bg_color=bg_color, flash_color=flash_color)

    def _make_scroll(self):
        sw = Gtk.ScrolledWindow(hexpand=True)
        sw.set_overflow(Gtk.Overflow.HIDDEN)
        sw.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_propagate_natural_height(True)
        sw.set_kinetic_scrolling(False)
        _suppress_overshoot(sw)
        if self._sw_width > 0:
            sw.set_min_content_width(self._sw_width)
            sw.set_max_content_width(self._sw_width)
            sw.set_propagate_natural_width(False)
            self.set_size_request(self._sw_width, -1)
        if self._sw_height > 0:
            sw.set_min_content_height(self._sw_height)
            sw.set_max_content_height(self._sw_height)
        if self._max_height is not None:
            sw.set_vexpand(True)
            sw.set_max_content_height(self._max_height)
        return sw

    def _get_adjustment(self):
        return self._scroll.get_vadjustment()

    def _draw(self, _area, cr, width, height, *_args):
        adj = self._get_adjustment()
        val = adj.get_value()
        upper = adj.get_upper()
        page = adj.get_page_size()
        gs = self._gradient_size
        fade_px = 40.0
        r, g, b = self._bg_color
        fr, fg, fb = self._flash_color
        radius = 0

        top_op = min(val / fade_px, 1.0)
        bottom_op = min((upper - page - val) / fade_px, 1.0)

        cr.save()
        self._rounded_rect(cr, 0, 0, width, height, radius)
        cr.clip()

        if top_op > 0:
            pat = cairo.LinearGradient(0, 0, 0, gs)
            pat.add_color_stop_rgba(0, r, g, b, top_op)
            pat.add_color_stop_rgba(1, r, g, b, 0.0)
            cr.rectangle(0, 0, width, gs)
            cr.set_source(pat)
            cr.fill()
        if bottom_op > 0:
            pat = cairo.LinearGradient(0, height - gs, 0, height)
            pat.add_color_stop_rgba(0, r, g, b, 0.0)
            pat.add_color_stop_rgba(1, r, g, b, bottom_op)
            cr.rectangle(0, height - gs, width, gs)
            cr.set_source(pat)
            cr.fill()

        if self._flash_opacity > 0:
            if self._flash_dir == -1:
                pat = cairo.LinearGradient(0, 0, 0, gs)
                pat.add_color_stop_rgba(
                    0, fr, fg, fb, self._flash_opacity)
                pat.add_color_stop_rgba(1, fr, fg, fb, 0.0)
                cr.rectangle(0, 0, width, gs)
                cr.set_source(pat)
                cr.fill()
            elif self._flash_dir == 1:
                pat = cairo.LinearGradient(
                    0, height - gs, 0, height)
                pat.add_color_stop_rgba(0, fr, fg, fb, 0.0)
                pat.add_color_stop_rgba(
                    1, fr, fg, fb, self._flash_opacity)
                cr.rectangle(0, height - gs, width, gs)
                cr.set_source(pat)
                cr.fill()

        cr.restore()


class VolumeSliderRow(Gtk.Box):
    """A volume row with a progress bar background and drag/scroll input."""

    def __init__(self, title, subtitle, index, initial_volume,
                 is_muted, set_volume_cb, set_mute_cb,
                 is_default=False, set_default_cb=None,
                 scroll_to_adjust=True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = index
        self.set_volume_cb = set_volume_cb
        self.set_mute_cb = set_mute_cb
        self.set_default_cb = set_default_cb
        self.is_muted = bool(is_muted)
        self.is_selected_item = False
        self.scroll_to_adjust = scroll_to_adjust

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
        self.dragging = False
        self.drag_gesture = Gtk.GestureDrag.new()
        self.drag_gesture.set_button(1)
        self.drag_gesture.connect("drag-begin", self.on_drag_begin)
        self.drag_gesture.connect("drag-update", self.on_drag_update)
        self.drag_gesture.connect("drag-end", self.on_drag_end)
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
        self.dragging = True
        self.update_volume_from_x(start_x)

    def on_drag_update(self, gesture, offset_x, offset_y):
        success, start_x, start_y = gesture.get_start_point()
        if success:
            self.update_volume_from_x(start_x + offset_x)

    def on_drag_end(self, gesture, offset_x, offset_y):
        self.dragging = False

    def on_right_click(self, gesture, n_press, x, y):
        self.toggle_mute()

    def on_scroll(self, controller, dx, dy):
        if not self.scroll_to_adjust:
            # Let the event bubble up to the parent ScrolledWindow
            return False
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
