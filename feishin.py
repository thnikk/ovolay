#!/usr/bin/python3
"""
Feishin remote client.

Runs a websocket connection to a Feishin remote-control server on a
background thread with its own asyncio loop, and marshals updates back
to the GTK main thread via GLib.idle_add.

Protocol (confirmed against feishin 1.17 remote server):
  - Playback state is pushed by the server: state/song/playback/position/
    volume/repeat/shuffle/favorite/rating/proxy/queue-state events.
  - The queue is NOT request/response: the server broadcasts `queue-state`
    on connect and on every queue mutation.
  - Library browsing (tracks/albums/playlists/radio) is request/response:
    send `tracks-request` with a requestId, get `tracks-response` back with
    the matching requestId.
  - Simple commands are fire-and-forget: play, pause, next, previous,
    volume, position (seek), play-track, queue-jump, proxy.
"""
import asyncio
import base64
import json
import threading
import uuid

import aiohttp
from gi.repository import GLib

RECONNECT_DELAY = 5

# Pushed by the server
EVENT_STATE = 'state'
EVENT_SONG = 'song'
EVENT_PLAYBACK = 'playback'
EVENT_POSITION = 'position'
EVENT_VOLUME = 'volume'
EVENT_REPEAT = 'repeat'
EVENT_SHUFFLE = 'shuffle'
EVENT_FAVORITE = 'favorite'
EVENT_RATING = 'rating'
EVENT_PROXY = 'proxy'
EVENT_QUEUE_STATE = 'queue-state'
EVENT_TRACKS_RESPONSE = 'tracks-response'

# Requested by the client
EVENT_TRACKS_REQUEST = 'tracks-request'
EVENT_PLAY_TRACK = 'play-track'
EVENT_QUEUE_JUMP = 'queue-jump'


class FeishinClient:
    """Background websocket client for a Feishin remote server."""

    def __init__(self, host='localhost', port=4333,
                 username='', password='', on_update=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        # Called on the GTK main thread as on_update(event, data)
        self._on_update = on_update
        self.state = {}
        self._loop = None
        self._ws = None
        self._stop = threading.Event()
        # requestId of the most recent tracks-request, used to filter
        # stale responses if the user types faster than the server answers
        self._search_request_id = None

    def ws_url(self):
        """Websocket URL for the remote server."""
        return f'ws://{self.host}:{self.port}/'

    def start(self):
        """Start the background connection thread."""
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        """Signal the background thread to disconnect and exit."""
        self._stop.set()
        ws, loop = self._ws, self._loop
        if ws and loop:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)

    # -- outgoing commands -------------------------------------------

    def send_event(self, event, **kwargs):
        """Send an event to the server; safe to call from any thread."""
        ws, loop = self._ws, self._loop
        if not ws or not loop:
            return
        payload = {'event': event, **kwargs}
        asyncio.run_coroutine_threadsafe(
            ws.send_str(json.dumps(payload)), loop)

    def play(self):
        """Resume playback."""
        self.send_event('play')

    def pause(self):
        """Pause playback."""
        self.send_event('pause')

    def toggle_play(self):
        """Toggle play/pause based on the last known status."""
        playing = self.state.get('status') == 'playing'
        self.send_event('pause' if playing else 'play')

    def next_track(self):
        """Skip to the next track."""
        self.send_event('next')

    def prev_track(self):
        """Skip to the previous track."""
        self.send_event('previous')

    def set_volume(self, volume):
        """Set playback volume, 0-100."""
        self.send_event(EVENT_VOLUME, volume=int(volume))

    def seek(self, position):
        """Seek to position in seconds."""
        self.send_event(EVENT_POSITION, position=float(position))

    def request_artwork(self):
        """Ask the server for the current song's artwork (base64)."""
        self.send_event(EVENT_PROXY)

    def search(self, query):
        """Ask the server for songs matching query.

        The answer arrives asynchronously as a `tracks-response` event
        carrying the same requestId.
        """
        self._search_request_id = uuid.uuid4().hex
        self.send_event(
            EVENT_TRACKS_REQUEST,
            requestId=self._search_request_id,
            searchTerm=query,
            limit=50,
            startIndex=0,
        )

    def play_song(self, song_id):
        """Play a specific song by id, e.g. from search."""
        self.send_event(EVENT_PLAY_TRACK, id=song_id)

    def play_queue_item(self, unique_id):
        """Jump playback to a specific queue item (by uniqueId)."""
        self.send_event(EVENT_QUEUE_JUMP, uniqueId=unique_id)

    # -- incoming messages ---------------------------------------------

    def _emit(self, event, data):
        """Schedule the update callback on the GTK main thread."""
        if self._on_update:
            GLib.idle_add(self._on_update, event, data)

    def _handle_message(self, message):
        """Update local state from one decoded server message."""
        event = message.get('event')
        data = message.get('data')
        known = (
            EVENT_STATE, EVENT_SONG, EVENT_PLAYBACK, EVENT_POSITION,
            EVENT_VOLUME, EVENT_REPEAT, EVENT_SHUFFLE, EVENT_FAVORITE,
            EVENT_RATING, EVENT_PROXY, EVENT_QUEUE_STATE,
            EVENT_TRACKS_RESPONSE,
        )
        if event not in known:
            return
        if event == EVENT_TRACKS_RESPONSE:
            # Ignore responses for searches that have since been superseded
            if data.get('requestId') != self._search_request_id:
                return
        self.state[event] = data
        self._emit(event, data)

    async def _client_loop(self):
        """Connect, authenticate, and read messages until disconnected."""
        auth_header = None
        if self.username or self.password:
            token = base64.b64encode(
                f'{self.username}:{self.password}'.encode()
            ).decode()
            auth_header = f'Basic {token}'

        self._loop = asyncio.get_running_loop()
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.ws_url()) as ws:
                self._ws = ws
                if auth_header:
                    await ws.send_str(json.dumps({
                        'event': 'authenticate',
                        'header': auth_header,
                    }))
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            self._handle_message(json.loads(msg.data))
                        except Exception as e:
                            print(f'feishin client message error: {e}')
                    elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                        break

    def _run(self):
        """Background thread body: reconnect until stop() is called."""
        while not self._stop.is_set():
            try:
                asyncio.run(self._client_loop())
            except Exception as e:
                print(f'feishin client connection error: {e}')
            self._ws = None
            self.state = {}
            if self._stop.is_set():
                break
            self._stop.wait(RECONNECT_DELAY)