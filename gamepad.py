#!/usr/bin/python3
# Gamepad input thread: watches evdev devices and maps controller
# buttons/axes to ovolay actions via GLib.idle_add callbacks.
import threading
import time

try:
    import evdev
    from evdev import ecodes
    _EVDEV_AVAILABLE = True
except ImportError:
    _EVDEV_AVAILABLE = False

from gi.repository import GLib

# ---------------------------------------------------------------------------
# Button / axis constants
# ---------------------------------------------------------------------------

# Combo: BTN_MODE (316) + BTN_SELECT (314) -> open ovolay
_BTN_MODE = 316
_BTN_SELECT = 314

# D-pad axes: navigate menu / adjust volume
_ABS_HAT0X = ecodes.ABS_HAT0X if _EVDEV_AVAILABLE else 16
_ABS_HAT0Y = ecodes.ABS_HAT0Y if _EVDEV_AVAILABLE else 17

# Shoulder buttons: switch tabs
_BTN_TL = 310
_BTN_TR = 311

# Action buttons
_BTN_NORTH = 307  # toggle mute
_BTN_SOUTH = 304  # set default input/output
_BTN_EAST = 305   # hide window

# Repeat settings for held d-pad
_REPEAT_INITIAL_DELAY = 0.4   # seconds before first repeat
_REPEAT_INTERVAL = 0.12       # seconds between repeats


class GamepadListener:
    """Monitor all connected gamepads and dispatch UI callbacks.

    Spawns one reader thread per device. A monitor thread watches
    udev for new devices and handles reconnects automatically.
    """

    def __init__(self, callbacks):
        """Initialise the listener.

        Parameters
        ----------
        callbacks : dict
            Mapping of action name to callable (called on GLib main
            loop). Keys: 'open', 'nav_up', 'nav_down', 'nav_left',
            'nav_right', 'tab_prev', 'tab_next', 'mute',
            'set_default', 'hide', 'music_prev', 'music_next',
            'music_vol_up', 'music_vol_down'.
        """
        if not _EVDEV_AVAILABLE:
            print(
                'gamepad: evdev not available, '
                'gamepad support disabled'
            )
            return
        self._cb = callbacks
        # Track which buttons are currently held {keycode: bool}
        self._held = {}
        # Active reader threads keyed by device path
        self._readers = {}
        self._lock = threading.Lock()
        # d-pad repeat state
        self._repeat_thread = None
        self._repeat_action = None
        self._repeat_stop = threading.Event()
        # Start monitor thread (also seeds existing devices)
        t = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name='gp-monitor',
        )
        t.start()

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    def _is_gamepad(self, device):
        """Return True if the device looks like a gamepad."""
        caps = device.capabilities()
        # Must have both key and absolute axis event types
        if ecodes.EV_KEY not in caps or ecodes.EV_ABS not in caps:
            return False
        keys = caps[ecodes.EV_KEY]
        # Must expose at least BTN_SOUTH (first standard gamepad btn)
        return ecodes.BTN_SOUTH in keys

    def _monitor_loop(self):
        """Seed existing devices then watch udev for hotplug events."""
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                if self._is_gamepad(dev):
                    self._start_reader(dev)
                else:
                    dev.close()
            except Exception:
                pass

        # Prefer evdev.DeviceMonitor for real-time hotplug detection;
        # fall back to periodic polling if unavailable.
        try:
            monitor = evdev.DeviceMonitor()
            monitor.start()
            for event in monitor.receive():
                if event.action == 'add':
                    time.sleep(0.2)  # let the device node settle
                    try:
                        dev = evdev.InputDevice(event.path)
                        if self._is_gamepad(dev):
                            self._start_reader(dev)
                        else:
                            dev.close()
                    except Exception:
                        pass
        except Exception:
            self._poll_loop()

    def _poll_loop(self):
        """Fallback: rescan /dev/input every 2 s for new gamepads."""
        while True:
            time.sleep(2)
            known = set(self._readers.keys())
            for path in evdev.list_devices():
                if path in known:
                    continue
                try:
                    dev = evdev.InputDevice(path)
                    if self._is_gamepad(dev):
                        self._start_reader(dev)
                    else:
                        dev.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Per-device reader
    # ------------------------------------------------------------------

    def _start_reader(self, device):
        """Spawn a daemon thread to read events from device."""
        path = device.path
        with self._lock:
            if path in self._readers:
                device.close()
                return
            self._readers[path] = True
        t = threading.Thread(
            target=self._read_loop,
            args=(device,),
            daemon=True,
            name=f'gp-reader-{path}',
        )
        t.start()

    def _read_loop(self, device):
        """Read events from a single device until it disconnects."""
        path = device.path
        print(f'gamepad: connected {device.name} ({path})')
        try:
            for event in device.read_loop():
                self._handle_event(event)
        except Exception:
            pass
        finally:
            print(f'gamepad: disconnected {path}')
            device.close()
            with self._lock:
                self._readers.pop(path, None)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _dispatch(self, action):
        """Schedule a callback on the GLib main loop."""
        cb = self._cb.get(action)
        if cb:
            GLib.idle_add(cb)

    def _handle_event(self, event):
        """Route a single evdev event to the appropriate action."""
        if event.type == ecodes.EV_KEY:
            self._handle_key(event.code, event.value)
        elif event.type == ecodes.EV_ABS:
            self._handle_abs(event.code, event.value)

    def _handle_key(self, code, value):
        """Handle button press (value=1) and release (value=0)."""
        pressed = value == 1
        self._held[code] = pressed

        if not pressed:
            return

        # --- Combo: BTN_MODE + BTN_SELECT -> open ---
        if code in (_BTN_MODE, _BTN_SELECT):
            if (self._held.get(_BTN_MODE)
                    and self._held.get(_BTN_SELECT)):
                self._dispatch('open')
            return

        # --- Shoulder buttons: cycle tabs ---
        if code == _BTN_TL:
            self._dispatch('tab_prev')
            return
        if code == _BTN_TR:
            self._dispatch('tab_next')
            return

        # --- Action buttons ---
        if code == _BTN_NORTH:
            self._dispatch('mute')
            return
        if code == _BTN_SOUTH:
            self._dispatch('south')
            return
        if code == _BTN_EAST:
            self._dispatch('hide')
            return

    def _handle_abs(self, code, value):
        """Handle hat/d-pad axis events with key-repeat."""
        if code == _ABS_HAT0Y:
            if value == -1:
                self._start_repeat('nav_up')
            elif value == 1:
                self._start_repeat('nav_down')
            else:
                self._stop_repeat()
        elif code == _ABS_HAT0X:
            if value == -1:
                self._start_repeat('nav_left')
            elif value == 1:
                self._start_repeat('nav_right')
            else:
                self._stop_repeat()

    # ------------------------------------------------------------------
    # D-pad key-repeat
    # ------------------------------------------------------------------

    def _start_repeat(self, action):
        """Fire action immediately then repeat while held."""
        self._stop_repeat()
        self._repeat_action = action
        self._repeat_stop.clear()
        self._dispatch(action)
        t = threading.Thread(
            target=self._repeat_loop,
            args=(action,),
            daemon=True,
            name='gp-repeat',
        )
        self._repeat_thread = t
        t.start()

    def _repeat_loop(self, action):
        """Sleep initial delay then repeat action until stopped."""
        if self._repeat_stop.wait(timeout=_REPEAT_INITIAL_DELAY):
            return
        while not self._repeat_stop.is_set():
            self._dispatch(action)
            self._repeat_stop.wait(timeout=_REPEAT_INTERVAL)

    def _stop_repeat(self):
        """Cancel any active repeat."""
        self._repeat_stop.set()
        self._repeat_thread = None
        self._repeat_action = None
