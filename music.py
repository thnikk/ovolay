import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Gio  # noqa

# CSS for the music tab; loaded by the caller alongside the main CSS
CSS = """
.music-art {
    background-color: alpha(#ffffff, 0.05);
    border-radius: 15px;
}

/*
.music-seekbar trough {
    min-height: 6px;
    border-radius: 3px;
    background-color: alpha(#ffffff, 0.1);
    border: none;
}

.music-seekbar highlight {
    background-color: @accent_bg_color;
    border-radius: 3px;
    border: none;
}

.music-seekbar slider {
    min-width: 14px;
    min-height: 14px;
    border-radius: 7px;
    background-color: #ffffff;
    border: none;
    box-shadow: none;
}
*/

.music-button {
    border-radius: 50%;
    background: transparent;
    box-shadow: none;
    border: none;
    padding: 6px;
}

.music-button:hover {
    background: alpha(currentColor, 0.1);
}

.play-button {
    padding: 10px;
}

.song-label {
    font-size: 28px;
}
.artist-label {
    font-size: 20px;
    opacity: 50%;
}

.music-time {
    font-size: 13px;
    opacity: 70%;
    font-variant-numeric: tabular-nums;
}

"""


class MusicTab(Gtk.Box):
    """MPRIS2 media player controls with album art."""

    ART_SIZE = 200

    def __init__(self, player_filter=None):
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=15)
        self.set_valign(Gtk.Align.CENTER)
        # Lowercase substring to match against the bus name suffix
        self._player_filter = (
            player_filter.lower() if player_filter else None)
        self._player = None
        self._dbus_proxy = None
        self._art_pixbuf = None
        self._seeking = False
        self._track_id = None
        # Guard flag to avoid feedback loop when updating volume bar
        self._vol_updating = False

        # Album art drawn with Cairo for rounded clipping
        self._art = Gtk.DrawingArea()
        self._art.set_size_request(self.ART_SIZE, self.ART_SIZE)
        self._art.set_valign(Gtk.Align.CENTER)
        self._art.add_css_class('music-art')
        self._art.set_draw_func(self._draw_art)
        self.append(self._art)

        # Right panel: title, artist, seekbar, buttons
        right = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)

        text_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        text_box.set_vexpand(True)

        self._title_lbl = Gtk.Label(label='Nothing playing')
        self._title_lbl.set_halign(Gtk.Align.START)
        self._title_lbl.set_ellipsize(3)
        self._title_lbl.add_css_class('song-label')
        text_box.append(self._title_lbl)

        self._artist_lbl = Gtk.Label(label='')
        self._artist_lbl.set_halign(Gtk.Align.START)
        self._artist_lbl.set_ellipsize(3)
        self._artist_lbl.add_css_class('artist-label')
        text_box.append(self._artist_lbl)

        right.append(text_box)

        # Seekbar
        self._seek_adj = Gtk.Adjustment(
            value=0, lower=0, upper=1,
            step_increment=1, page_increment=10)
        self._seekbar = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self._seek_adj)
        self._seekbar.set_draw_value(False)
        self._seekbar.set_hexpand(True)
        self._seekbar.add_css_class('music-seekbar')
        press = Gtk.GestureClick.new()
        press.connect('pressed', self._on_seek_press)
        press.connect('released', self._on_seek_release)
        self._seekbar.add_controller(press)
        right.append(self._seekbar)

        # Bottom row: time | buttons | volume
        btn_row = Gtk.CenterBox()
        btn_row.set_valign(Gtk.Align.CENTER)

        # Left: elapsed/total time display
        self._time_lbl = Gtk.Label(label='0:00/0:00')
        self._time_lbl.set_valign(Gtk.Align.CENTER)
        self._time_lbl.add_css_class('music-time')
        btn_row.set_start_widget(self._time_lbl)

        # Center: prev / play / next
        btns = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        btns.set_valign(Gtk.Align.CENTER)
        self._prev_btn = self._make_btn(
            'media-skip-backward-symbolic', self._cmd_prev)
        self._play_btn = self._make_btn(
            'media-playback-start-symbolic',
            self._cmd_play_pause, icon_size=24)
        self._play_btn.add_css_class('play-button')
        self._next_btn = self._make_btn(
            'media-skip-forward-symbolic', self._cmd_next)
        btns.append(self._prev_btn)
        btns.append(self._play_btn)
        btns.append(self._next_btn)
        btn_row.set_center_widget(btns)

        # Right: volume scale with speaker icon
        vol_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vol_box.set_valign(Gtk.Align.CENTER)
        self._vol_adj = Gtk.Adjustment(
            value=100, lower=0, upper=100,
            step_increment=1, page_increment=5)
        self._vol_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self._vol_adj)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.set_hexpand(True)
        self._vol_scale.set_size_request(80, -1)
        self._vol_scale.set_valign(Gtk.Align.CENTER)
        self._vol_scale.add_css_class('music-seekbar')
        self._vol_scale.connect(
            'value-changed', self._on_vol_changed)
        vol_box.append(self._vol_scale)

        btn_row.set_end_widget(vol_box)
        right.append(btn_row)

        self.append(right)
        self._init_dbus()
        # Poll position and status every second
        GLib.timeout_add(1000, self._poll)

    def _make_btn(self, icon, callback, icon_size=16):
        """Create a circular icon button with optional icon size."""
        btn = Gtk.Button()
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(icon_size)
        btn.set_child(img)
        btn.add_css_class('music-button')
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_halign(Gtk.Align.CENTER)
        # Fix size so the hover highlight stays circular
        size = icon_size + 24
        btn.set_size_request(size, size)
        btn.connect('clicked', lambda b: callback())
        return btn

    # ------------------------------------------------------------------
    # Public keybind interface (called from VolumeOverlay)
    # ------------------------------------------------------------------

    def cmd_prev(self):
        """Skip to the previous track."""
        self._cmd_prev()

    def cmd_next(self):
        """Skip to the next track."""
        self._cmd_next()

    def adjust_volume(self, delta):
        """Adjust MPRIS2 volume by delta (fraction, e.g. 0.05)."""
        if self._player is None:
            return
        current = self._vol_adj.get_value() / 100.0
        new_vol = max(0.0, min(1.0, current + delta))
        # Update the scale; _on_vol_changed will forward to MPRIS2
        self._vol_updating = False
        self._vol_adj.set_value(new_vol * 100)

    # ------------------------------------------------------------------
    # D-Bus / MPRIS2
    # ------------------------------------------------------------------

    def _init_dbus(self):
        """Enumerate existing MPRIS2 players and watch for new ones."""
        # Watch for players appearing/disappearing via NameOwnerChanged
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            'org.freedesktop.DBus',
            '/org/freedesktop/DBus',
            'org.freedesktop.DBus',
            None,
            self._on_dbus_proxy_ready,
        )

    def _on_dbus_proxy_ready(self, source, result):
        """Finish async DBus proxy creation; list names and subscribe."""
        try:
            self._dbus_proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except Exception as e:
            print(f'music tab dbus proxy error: {e}')
            return
        # Connect to NameOwnerChanged so we track players appearing/leaving
        self._dbus_proxy.connect('g-signal', self._on_dbus_signal)
        # List current names to find any already-running players
        try:
            result = self._dbus_proxy.call_sync(
                'ListNames', None,
                Gio.DBusCallFlags.NONE, -1, None)
            names = result.unpack()[0]
            for name in names:
                if self._matches_player(name):
                    self._connect_player(name)
                    break
        except Exception as e:
            print(f'music tab list names error: {e}')

    def _matches_player(self, bus_name):
        """Return True if bus_name is an MPRIS2 player matching filter."""
        prefix = 'org.mpris.MediaPlayer2.'
        if not bus_name.startswith(prefix):
            return False
        if self._player_filter is None:
            return True
        suffix = bus_name[len(prefix):].lower()
        return self._player_filter in suffix

    def _on_dbus_signal(self, proxy, sender, signal, params):
        """Handle NameOwnerChanged to track MPRIS2 players."""
        if signal != 'NameOwnerChanged':
            return
        name, old_owner, new_owner = params.unpack()
        if not self._matches_player(name):
            return
        if new_owner and self._player is None:
            self._connect_player(name)
        elif not new_owner and old_owner:
            self._clear_player()

    def _connect_player(self, bus_name):
        """Create an async Gio.DBusProxy for the MPRIS2 player."""
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            bus_name,
            '/org/mpris/MediaPlayer2',
            'org.mpris.MediaPlayer2.Player',
            None,
            self._on_player_proxy_ready,
        )

    def _on_player_proxy_ready(self, source, result):
        """Finish async player proxy creation and load metadata."""
        try:
            self._player = Gio.DBusProxy.new_for_bus_finish(result)
        except Exception as e:
            print(f'music tab player proxy error: {e}')
            return
        # PropertiesChanged signals fire when track or state changes
        self._player.connect(
            'g-properties-changed', self._on_properties_changed)
        self._update_metadata()

    def _on_properties_changed(self, proxy, changed, invalidated):
        """Refresh UI when MPRIS2 properties change."""
        self._update_metadata()

    def _clear_player(self):
        """Remove the current player reference and reset the UI."""
        self._player = None
        self._art_pixbuf = None
        self._track_id = None
        self._title_lbl.set_text('Nothing playing')
        self._artist_lbl.set_text('')
        self._seek_adj.set_upper(1)
        self._seek_adj.set_value(0)
        self._time_lbl.set_text('0:00/0:00')
        self._art.queue_draw()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _get_prop(self, proxy, name, default=None):
        """Read a cached property from a Gio.DBusProxy as Python."""
        variant = proxy.get_cached_property(name)
        if variant is None:
            return default
        return variant.unpack()

    def _update_metadata(self):
        """Refresh title, artist, art, duration, and volume."""
        if self._player is None:
            return
        try:
            meta = self._get_prop(self._player, 'Metadata', {})
            # xesam:title may itself be a variant; unpack if needed
            title_v = meta.get('xesam:title')
            title = (
                title_v.unpack() if hasattr(title_v, 'unpack')
                else title_v
            ) or 'Unknown'

            artists_v = meta.get('xesam:artist')
            artists = (
                artists_v.unpack() if hasattr(artists_v, 'unpack')
                else artists_v
            ) or []
            if isinstance(artists, (list, tuple)):
                artist = ', '.join(str(a) for a in artists)
            else:
                artist = str(artists)

            length_v = meta.get('mpris:length')
            length = (
                length_v.unpack() if hasattr(length_v, 'unpack')
                else length_v
            ) or 0

            track_id_v = meta.get('mpris:trackid')
            self._track_id = (
                track_id_v.unpack() if hasattr(track_id_v, 'unpack')
                else track_id_v
            )

            self._title_lbl.set_text(str(title))
            self._artist_lbl.set_text(str(artist))
            self._seek_adj.set_upper(max(1, length / 1_000_000))

            art_url_v = meta.get('mpris:artUrl')
            art_url = (
                art_url_v.unpack() if hasattr(art_url_v, 'unpack')
                else art_url_v
            ) or ''
            self._load_art(art_url)

            status = self._get_prop(
                self._player, 'PlaybackStatus', 'Stopped')
            self._update_play_icon(status)
            self._sync_volume_bar()
        except Exception as e:
            print(f'music tab metadata error: {e}')

    def _update_play_icon(self, status):
        """Switch the play button icon to match playback state."""
        icon = (
            'media-playback-pause-symbolic'
            if status == 'Playing'
            else 'media-playback-start-symbolic'
        )
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(32)
        self._play_btn.set_child(img)

    def _sync_volume_bar(self):
        """Read MPRIS2 Volume and update the scale without feedback."""
        if self._player is None:
            return
        vol = self._get_prop(self._player, 'Volume', None)
        if vol is not None and isinstance(vol, float):
            self._vol_updating = True
            self._vol_adj.set_value(vol * 100)
            self._vol_updating = False

    # ------------------------------------------------------------------
    # Album art
    # ------------------------------------------------------------------

    def _load_art(self, url):
        """Load album art from a file:// or https:// URL."""
        self._art_pixbuf = None
        if not url:
            self._art.queue_draw()
            return
        if url.startswith('file://'):
            self._load_art_from_path(url[7:])
        else:
            # Fetch remote art asynchronously via Gio
            gfile = Gio.File.new_for_uri(url)
            gfile.load_contents_async(
                None, self._on_art_loaded, url)

    def _load_art_from_path(self, path):
        """Load album art from a local filesystem path."""
        try:
            self._art_pixbuf = (
                GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, self.ART_SIZE, self.ART_SIZE, True))
        except Exception as e:
            print(f'music tab art load error: {e}')
        self._art.queue_draw()

    def _on_art_loaded(self, gfile, result, url):
        """Callback for async remote art fetch; decode bytes to pixbuf."""
        try:
            ok, data, _ = gfile.load_contents_finish(result)
            if not ok or not data:
                self._art.queue_draw()
                return
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(bytes(data))
            loader.close()
            pb = loader.get_pixbuf()
            if pb:
                self._art_pixbuf = pb.scale_simple(
                    self.ART_SIZE, self.ART_SIZE,
                    GdkPixbuf.InterpType.BILINEAR)
        except Exception as e:
            print(f'music tab remote art error: {e}')
        self._art.queue_draw()

    def _rounded_rect(self, cr, x, y, w, h, r):
        """Trace a rounded rectangle path on the Cairo context."""
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.close_path()

    def _draw_art(self, area, cr, width, height):
        """Draw album art clipped to a rounded rectangle."""
        cr.save()
        self._rounded_rect(cr, 0, 0, width, height, 15)
        cr.clip()
        if self._art_pixbuf:
            # Scale to fill the square and paint
            pb = self._art_pixbuf.scale_simple(
                width, height,
                GdkPixbuf.InterpType.BILINEAR)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
        else:
            # Placeholder fill
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.paint()
        cr.restore()

    # ------------------------------------------------------------------
    # Seekbar
    # ------------------------------------------------------------------

    def _on_seek_press(self, gesture, n, x, y):
        self._seeking = True

    def _on_seek_release(self, gesture, n, x, y):
        """Seek to the scale's current position."""
        if self._player and self._track_id:
            pos_us = int(self._seek_adj.get_value() * 1_000_000)
            try:
                self._player.SetPosition(self._track_id, pos_us)
            except Exception as e:
                print(f'music tab seek error: {e}')
        self._seeking = False

    # ------------------------------------------------------------------
    # Volume bar
    # ------------------------------------------------------------------

    def _on_vol_changed(self, scale):
        """Send new volume to MPRIS2 player when the slider moves."""
        if self._vol_updating or self._player is None:
            return
        vol = self._vol_adj.get_value() / 100.0
        self._player.call(
            'org.freedesktop.DBus.Properties.Set',
            GLib.Variant('(ssv)', (
                'org.mpris.MediaPlayer2.Player',
                'Volume',
                GLib.Variant('d', vol),
            )),
            Gio.DBusCallFlags.NONE, -1, None, None, None)

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_time(seconds):
        """Format seconds as M:SS."""
        s = int(seconds)
        return f'{s // 60}:{s % 60:02d}'

    def _poll(self):
        """Poll playback position, status, and volume every second."""
        if self._player and not self._seeking:
            try:
                pos_v = self._player.call_sync(
                    'org.freedesktop.DBus.Properties.Get',
                    GLib.Variant('(ss)', (
                        'org.mpris.MediaPlayer2.Player', 'Position')),
                    Gio.DBusCallFlags.NONE, -1, None)
                pos = pos_v.unpack()[0] / 1_000_000
                self._seek_adj.set_value(pos)

                total = self._seek_adj.get_upper()
                self._time_lbl.set_text(
                    f'{self._fmt_time(pos)}/{self._fmt_time(total)}'
                )

                status = self._get_prop(
                    self._player, 'PlaybackStatus', 'Stopped')
                self._update_play_icon(status)
                self._sync_volume_bar()
            except Exception:
                pass
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------------
    # Playback commands
    # ------------------------------------------------------------------

    def _call(self, method):
        """Fire-and-forget async call on the player proxy."""
        if self._player is None:
            return
        self._player.call(
            method, None,
            Gio.DBusCallFlags.NONE, -1, None, None, None)

    def _cmd_prev(self):
        self._call('Previous')

    def _cmd_play_pause(self):
        self._call('PlayPause')
        # Refresh icon shortly after to reflect new state
        GLib.timeout_add(150, self._update_metadata)

    def _cmd_next(self):
        self._call('Next')
