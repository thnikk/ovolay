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

# Priority used when picking a fallback after the active player exits
_STATUS_PRIORITY = {'Playing': 0, 'Paused': 1, 'Stopped': 2}


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
        # All live proxies keyed by bus name
        self._proxies = {}
        # Bus name of the player currently shown in the UI
        self._last_played = None
        self._dbus_proxy = None
        self._seeking = False
        self._track_id = None
        # Guard flag to avoid feedback loop when updating volume bar
        self._vol_updating = False
        # Cache last art URL to avoid redundant reloads
        self._art_url = None

        # Album art displayed with Gtk.Picture (avoids Cairo/pycairo)
        self._art = Gtk.Picture()
        self._art.set_size_request(self.ART_SIZE, self.ART_SIZE)
        self._art.set_valign(Gtk.Align.CENTER)
        self._art.set_can_shrink(True)
        self._art.set_content_fit(Gtk.ContentFit.COVER)
        self._art.add_css_class('music-art')
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
        self._seekbar.set_focusable(False)
        # change-value fires on every user-driven drag step; use it to
        # set the seeking flag and debounce the actual SetPosition call
        self._seekbar.connect('change-value', self._on_seek_change)
        self._seek_timer_id = None
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
            self._cmd_play_pause, icon_size=20)
        self._play_btn.add_css_class('play-button')
        self._next_btn = self._make_btn(
            'media-skip-forward-symbolic', self._cmd_next)
        btns.append(self._prev_btn)
        btns.append(self._play_btn)
        btns.append(self._next_btn)
        btn_row.set_center_widget(btns)

        # Right: volume scale
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
        self._vol_scale.set_focusable(False)
        self._vol_scale.connect('value-changed', self._on_vol_changed)
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
        btn.set_focusable(False)
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
        proxy = self._proxies.get(self._last_played)
        if proxy is None:
            return
        current = self._vol_adj.get_value() / 100.0
        new_vol = max(0.0, min(1.0, current + delta))
        self._vol_updating = False
        self._vol_adj.set_value(new_vol * 100)

    # ------------------------------------------------------------------
    # D-Bus / MPRIS2
    # ------------------------------------------------------------------

    def _init_dbus(self):
        """Connect to the session bus and start watching MPRIS2 players."""
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
        """Subscribe to NameOwnerChanged and add proxies for all players."""
        try:
            self._dbus_proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except Exception as e:
            print(f'music tab dbus proxy error: {e}')
            return
        self._dbus_proxy.connect('g-signal', self._on_dbus_signal)
        # Add a proxy for every already-running matching player
        try:
            res = self._dbus_proxy.call_sync(
                'ListNames', None,
                Gio.DBusCallFlags.NONE, -1, None)
            for name in res.unpack()[0]:
                if self._matches_player(name):
                    self._add_player(name)
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
        """Handle NameOwnerChanged to track players appearing/leaving."""
        if signal != 'NameOwnerChanged':
            return
        name, old_owner, new_owner = params.unpack()
        if not self._matches_player(name):
            return
        if new_owner:
            self._add_player(name)
        elif old_owner:
            self._remove_player(name)

    def _add_player(self, bus_name):
        """Create an async proxy for a newly seen player."""
        if bus_name in self._proxies:
            return
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            bus_name,
            '/org/mpris/MediaPlayer2',
            'org.mpris.MediaPlayer2.Player',
            None,
            self._on_player_proxy_ready,
            bus_name,
        )

    def _on_player_proxy_ready(self, source, result, bus_name):
        """Store the proxy, subscribe to changes, and seed the active player."""
        try:
            proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except Exception as e:
            print(f'music tab player proxy error: {e}')
            return
        self._proxies[bus_name] = proxy
        proxy.connect(
            'g-properties-changed',
            self._on_properties_changed,
            bus_name,
        )
        # If nothing is displayed yet, show this player immediately;
        # it will be superseded if a Playing player appears later
        if self._last_played is None:
            self._last_played = bus_name
            self._refresh_ui(proxy)
        # If this player is already Playing, take over the display
        status = self._get_prop(proxy, 'PlaybackStatus')
        if status == 'Playing':
            self._last_played = bus_name
            self._refresh_ui(proxy)

    def _remove_player(self, bus_name):
        """Drop a proxy and switch display if it was the active player."""
        self._proxies.pop(bus_name, None)
        if bus_name != self._last_played:
            return
        # Active player gone; pick the best remaining one
        best_name = None
        best_priority = 99
        for name, proxy in self._proxies.items():
            status = self._get_prop(proxy, 'PlaybackStatus', 'Stopped')
            priority = _STATUS_PRIORITY.get(status, 3)
            if priority < best_priority:
                best_priority = priority
                best_name = name
        self._last_played = best_name
        if best_name is not None:
            self._refresh_ui(self._proxies[best_name])
        else:
            self._clear_ui()

    def _on_properties_changed(self, proxy, changed, invalidated,
                               bus_name):
        """Switch active player on Playing; refresh UI for active player."""
        status = self._get_prop(proxy, 'PlaybackStatus')
        if status == 'Playing' and bus_name != self._last_played:
            # A different player started playing; switch to it
            self._last_played = bus_name
        if bus_name == self._last_played:
            self._refresh_ui(proxy)

    # ------------------------------------------------------------------
    # UI refresh
    # ------------------------------------------------------------------

    def _get_prop(self, proxy, name, default=None):
        """Read a cached property from a Gio.DBusProxy as Python."""
        variant = proxy.get_cached_property(name)
        if variant is None:
            return default
        return variant.unpack()

    def _refresh_ui(self, proxy):
        """Update all UI elements from the given player proxy."""
        try:
            meta = self._get_prop(proxy, 'Metadata', {})

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
            if art_url != self._art_url:
                self._art_url = art_url
                self._load_art(art_url)

            status = self._get_prop(proxy, 'PlaybackStatus', 'Stopped')
            self._update_play_icon(status)
            self._sync_volume_bar(proxy)
        except Exception as e:
            print(f'music tab ui refresh error: {e}')

    def _clear_ui(self):
        """Reset the UI to the idle/no-player state."""
        self._track_id = None
        self._art_url = None
        self._title_lbl.set_text('Nothing playing')
        self._artist_lbl.set_text('')
        self._seek_adj.set_upper(1)
        self._seek_adj.set_value(0)
        self._time_lbl.set_text('0:00/0:00')
        self._art.set_paintable(None)

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

    def _sync_volume_bar(self, proxy):
        """Read MPRIS2 Volume from proxy and update the scale."""
        vol = self._get_prop(proxy, 'Volume', None)
        if vol is not None and isinstance(vol, float):
            self._vol_updating = True
            self._vol_adj.set_value(vol * 100)
            self._vol_updating = False

    # ------------------------------------------------------------------
    # Album art
    # ------------------------------------------------------------------

    def _load_art(self, url):
        """Load album art from a file:// or https:// URL."""
        if not url:
            self._art.set_paintable(None)
            return
        if url.startswith('file://'):
            self._load_art_from_path(url[7:])
        else:
            gfile = Gio.File.new_for_uri(url)
            gfile.load_contents_async(
                None, self._on_art_loaded, url)

    def _load_art_from_path(self, path):
        """Load album art from a local filesystem path."""
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self.ART_SIZE, self.ART_SIZE, True)
            texture = Gdk.Texture.new_for_pixbuf(pb)
            self._art.set_paintable(texture)
        except Exception as e:
            print(f'music tab art load error: {e}')

    def _on_art_loaded(self, gfile, result, url):
        """Callback for async remote art fetch; decode bytes to pixbuf."""
        try:
            ok, data, _ = gfile.load_contents_finish(result)
            if not ok or not data:
                return
            loader = GdkPixbuf.PixbufLoader.new()
            loader.write(bytes(data))
            loader.close()
            pb = loader.get_pixbuf()
            if pb:
                texture = Gdk.Texture.new_for_pixbuf(pb)
                self._art.set_paintable(texture)
        except Exception as e:
            print(f'music tab remote art error: {e}')

    # ------------------------------------------------------------------
    # Seekbar
    # ------------------------------------------------------------------

    def _on_seek_change(self, scale, scroll_type, value):
        """Handle user-driven seekbar movement with debounce."""
        self._seeking = True
        if self._seek_timer_id is not None:
            GLib.source_remove(self._seek_timer_id)
        self._seek_timer_id = GLib.timeout_add(
            150, self._do_seek, value)

    def _do_seek(self, value):
        """Send SetPosition to the active player and clear seeking flag."""
        self._seek_timer_id = None
        proxy = self._proxies.get(self._last_played)
        if proxy and self._track_id:
            pos_us = int(value * 1_000_000)
            try:
                proxy.call(
                    'SetPosition',
                    GLib.Variant('(ox)', (self._track_id, pos_us)),
                    Gio.DBusCallFlags.NONE, -1, None, None, None)
            except Exception as e:
                print(f'music tab seek error: {e}')
        self._seeking = False
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Volume bar
    # ------------------------------------------------------------------

    def _on_vol_changed(self, scale):
        """Send new volume to the active MPRIS2 player."""
        if self._vol_updating:
            return
        proxy = self._proxies.get(self._last_played)
        if proxy is None:
            return
        vol = self._vol_adj.get_value() / 100.0
        proxy.call(
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
        """Poll playback position and status every second."""
        proxy = self._proxies.get(self._last_played)
        if proxy and not self._seeking:
            try:
                pos_v = proxy.call_sync(
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
                    proxy, 'PlaybackStatus', 'Stopped')
                self._update_play_icon(status)
                self._sync_volume_bar(proxy)
            except Exception:
                pass
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------------
    # Playback commands
    # ------------------------------------------------------------------

    def _call(self, method):
        """Fire-and-forget async call on the active player proxy."""
        proxy = self._proxies.get(self._last_played)
        if proxy is None:
            return
        proxy.call(
            method, None,
            Gio.DBusCallFlags.NONE, -1, None, None, None)

    def _cmd_prev(self):
        self._call('Previous')

    def _cmd_play_pause(self):
        self._call('PlayPause')
        # Refresh icon shortly after to reflect new state
        proxy = self._proxies.get(self._last_played)
        if proxy:
            GLib.timeout_add(150, self._refresh_ui, proxy)

    def _cmd_next(self):
        self._call('Next')
