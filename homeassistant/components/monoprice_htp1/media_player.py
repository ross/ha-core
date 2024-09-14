"""Monoprice HTP-1 Media Player."""

import asyncio
from json import dumps, loads
import logging

import aiohttp

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

_LOGGER.debug = _LOGGER.info


def _dumps(data, *args, **kwargs):
    return dumps(data, *args, separators=(",", ":"), **kwargs)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class Htp1:
    def __init__(self, hostname):
        _LOGGER.debug("__init__: hostname=%s", hostname)
        self.hostname = hostname

        self._subscriptions = {}

        # current state and queued up changes
        self._state = None
        self._tx = None

        # connection bits
        self._client = None
        self._ws = None

        # our main background task
        self._task = None
        # a flag things can wait on/check to see if we're ready, meaning that
        # we've received the initial state (mso)
        self.ready = asyncio.Event()

    async def connect(self):
        if self._client is not None:
            return
        _LOGGER.debug("connect:")
        try:
            self._client = aiohttp.ClientSession(f"ws://{self.hostname}")
            self._ws = await self._client.ws_connect("/ws/controller")
        except (aiodns.error.DNSError, aiohttp.client_exceptions.ClientError) as e:
            self._client = self._ws = None
            raise CannotConnect()

    def start(self):
        _LOGGER.debug("start:")
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        _LOGGER.debug("_run: connecting")

        await self.connect()
        ws = self._ws

        _LOGGER.debug("_run:   requesting initial state")
        await ws.send_str("getmso")

        _LOGGER.debug("_run:   entering loop")
        while not ws.closed:
            try:
                msg = await ws.receive_str()
            except TypeError:
                # wasn't a TXT message, we're not interested
                continue
            _LOGGER.debug("_run:   received msg[:32]=%s***", msg[:32])
            # split off the command from the payload
            cmd, payload = msg.split(" ", 1)
            # see if we are interested in the command
            handler = getattr(self, f"_cmd_{cmd}", None)
            if handler:
                # parse the (json) payload
                payload = loads(payload)
                try:
                    await handler(payload)
                except Exception:
                    _LOGGER.exception("_run: handler=%s, threw an exception")
        _LOGGER.debug("_run:   exited loop")

    async def stop(self):
        _LOGGER.debug("stop:")
        if self._ws is not None:
            _LOGGER.debug("stop:   closing websocket")
            await self._ws.close()
            self._ws = None
        if self._client is not None:
            _LOGGER.debug("stop:   closing client")
            await self._client.close()
            self._client = None
        if self._task is not None:
            _LOGGER.debug("stop:   waiting for task")
            await self._task
            self._task = None
        _LOGGER.debug("stop:   complete")

    ## Subscriptions

    def subscribe(self, path, callback):
        if path not in self._subscriptions:
            self._subscriptions[path] = []

        self._subscriptions[path].append(callback)

    ## Handlers

    async def _cmd_mso(self, payload):
        _LOGGER.debug("_cmd_mso: payload=***")
        self._state = payload
        self.ready.set()

    async def _cmd_msoupdate(self, payload):
        if not isinstance(payload, list):
            payload = [payload]
        _LOGGER.debug("_cmd_msoupdate: len(payload)=%d", len(payload))
        for piece in payload:
            op = piece["op"]
            path = piece["path"][1:].split("/")
            value = piece["value"]
            _LOGGER.debug("_cmd_msoupdate:   op=%s, path=%s, value=%s", op, path, value)
            d = self._state
            last = path.pop()
            if op in ("add", "replace"):
                # for now we're assuming adds are a mistake as we don't expect
                # new nodes to show up, same for deletes/removes.
                for node in path:
                    if isinstance(d, list):
                        node = int(node)
                    d = d[node]
            else:
                raise NotImplementedError

            # make the change
            d[last] = value

            # notify anyone who's interested
            subscribers = self._subscriptions.get(piece["path"]) or []
            for subscriber in subscribers:
                await subscriber(value)

    ## Async ContextManager

    async def __aenter__(self):
        if self._tx is not None:
            raise Exception("transaction already in progress")
        self._tx = {}
        return self

    async def __aexit__(self, exc_type=None, exc_val=None, exc_tb=None):
        self._tx = None

    async def commit(self):
        _LOGGER.debug("commit: _tx=%s", self._tx)
        if not self._tx:
            return False

        ops = [
            {
                "op": "replace",
                "path": k,
                "value": v,
            }
            for k, v in self._tx.items()
        ]
        payload = _dumps(ops)
        await self._ws.send_str(f"changemso {payload}")

        self._tx = {}
        return True

    ## Operations

    @property
    def serial_number(self):
        return str(self._state["versions"]["SerialNumber"])

    @property
    def cal_vph(self):
        return self._state["cal"]["vph"]

    @property
    def cal_vpl(self):
        return self._state["cal"]["vpl"]

    @property
    def muted(self):
        try:
            return self._tx["/muted"]
        except (TypeError, KeyError):
            return self._state["muted"]

    @muted.setter
    def muted(self, value):
        if self._tx is None:
            raise Exception("no transaction in progress")
        self._tx["/muted"] = value

    @property
    def volume(self):
        try:
            return self._tx["/volume"]
        except (TypeError, KeyError):
            return self._state["volume"]

    @volume.setter
    def volume(self, value):
        if self._tx is None:
            raise Exception("no transaction in progress")
        self._tx["/volume"] = value

    @property
    def power(self):
        try:
            return self._tx["/powerIsOn"]
        except (TypeError, KeyError):
            return self._state["powerIsOn"]

    @power.setter
    def power(self, value):
        if self._tx is None:
            raise Exception("no transaction in progress")
        self._tx["/powerIsOn"] = value

    @property
    def mute(self):
        try:
            return self._tx["/muted"]
        except (TypeError, KeyError):
            return self._state["muted"]

    @mute.setter
    def mute(self, value):
        if self._tx is None:
            raise Exception("no transaction in progress")
        self._tx["/muted"] = value

    @property
    def input(self):
        try:
            _id = self._tx["/input"]
        except (TypeError, KeyError):
            _id = self._state["input"]
        return self._state["inputs"][_id]["label"]

    @input.setter
    def input(self, value):
        if self._tx is None:
            raise Exception("no transaction in progress")
        for _id, info in self._state["inputs"].items():
            if value == info["label"]:
                self._tx["/input"] = _id
                return
        raise Exception("input '{value}' not found")

    @property
    def inputs(self):
        if not self._state:
            return []
        return sorted(
            i["label"] for i in self._state["inputs"].values() if i["visible"]
        )

    @property
    def upmix(self):
        try:
            return self._tx["/upmix/select"]
        except (TypeError, KeyError):
            return self._state["upmix"]["select"]

    @upmix.setter
    def upmix(self, value):
        if self._tx is None:
            raise Exception("no transaction in progress")
        self._tx["/upmix/select"] = value

    @property
    def upmixes(self):
        if not self._state:
            return []
        return sorted(
            k for k, i in self._state["upmix"].items() if k != "select" and i["homevis"]
        )


class Htp1MediaPlayer(MediaPlayerEntity):
    def __init__(self, htp1):
        self.htp1 = htp1
        self._state = None

    async def async_added_to_hass(self) -> None:
        """Run when this Entity has been added to HA."""
        await super().async_added_to_hass()

        htp1 = self.htp1

        htp1.subscribe("/muted", self._updated)
        htp1.subscribe("/powerIsOn", self._updated)
        htp1.subscribe("/volume", self._updated)

        # TODO: need to try and reconnect if things disconnect etc
        htp1.start()
        _LOGGER.debug("async_added_to_hass: started & waiting for ready")
        await htp1.ready.wait()

    async def async_will_remove_from_hass(self) -> None:
        """Entity being removed from hass."""
        await super().async_will_remove_from_hass()

        _LOGGER.debug("async_will_remove_from_hass: stopping")
        await self.htp1.stop()

    async def _updated(self, *args, **kwargs):
        # https://developers.home-assistant.io/docs/integration_fetching_data/
        _LOGGER.debug("_updated")
        self.async_write_ha_state()

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Flag media player features that are supported."""
        return (
            MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.SELECT_SOUND_MODE
            | MediaPlayerEntityFeature.SELECT_SOURCE
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_STEP
        )

    @property
    def unique_id(self) -> str:
        """Return the unique ID for this media_player."""
        return self.htp1.serial_number

    ## Power

    async def async_turn_on(self) -> None:
        """Turn the media player on."""
        _LOGGER.debug("async_turn_on:")
        async with self.htp1 as tx:
            tx.power = True
            await tx.commit()

    async def async_turn_off(self) -> None:
        """Turn the media player off."""
        _LOGGER.debug("async_turn_off:")
        async with self.htp1 as tx:
            tx.power = False
            await tx.commit()

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the player."""
        return MediaPlayerState.ON if self.htp1.power else MediaPlayerState.OFF

    ## Volume

    @property
    def volume_level(self):
        """Return the volume level of the media player (0..1)."""
        volume = self.htp1.volume
        return (volume - self.htp1.cal_vpl) / (self.htp1.cal_vph - self.htp1.cal_vpl)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume level, range 0..1."""
        volume = volume * (self.htp1.cal_vph - self.htp1.cal_vpl) + self.htp1.cal_vpl
        async with self.htp1 as tx:
            tx.volume = volume
            await tx.commit()

    async def async_volume_up(self) -> None:
        """Turn volume up for media player."""
        async with self.htp1 as tx:
            # TODO: what if we get this again before changemso
            tx.volume = tx.volume + 1
            await tx.commit()

    async def async_volume_down(self) -> None:
        """Turn volume down for media player."""
        async with self.htp1 as tx:
            # TODO: what if we get this again before changemso
            tx.volume = tx.volume - 1
            await tx.commit()

    ## Mute

    @property
    def is_volume_muted(self):
        return self.htp1.muted

    async def async_mute_volume(self, mute: bool) -> None:
        async with self.htp1 as tx:
            tx.muted = mute
            await tx.commit()

    ## Sound Mode

    async def async_select_sound_mode(self, sound_mode: str) -> None:
        """Select sound mode."""
        async with self.htp1 as tx:
            tx.upmix = sound_mode
            await tx.commit()

    @property
    def sound_mode(self):
        """Return the current sound mode."""
        return self.htp1.upmix

    @property
    def sound_mode_list(self):
        """Return a list of available sound modes."""
        return self.htp1.upmixes

    ## Source

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        async with self.htp1 as tx:
            tx.input = source
            await tx.commit()

    @property
    def source_id(self):
        """ID of the current input source."""
        return self.htp1.input

    @property
    def source(self):
        """Name of the current input source."""
        return self.htp1.input

    @property
    def source_list(self):
        """List of available input sources."""
        return self.htp1.inputs


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Monoprice HTP-1 platform."""

    hostname = config_entry.data[CONF_HOST]
    name = config_entry.data.get(CONF_NAME, config_entry.data[CONF_HOST])

    # TODO: follow the coodinator <-> entity pattern
    # https://developers.home-assistant.io/docs/integration_fetching_data

    htp1 = Htp1(hostname=hostname)
    try:
        htp1.connect()
    except CannotConnect as e:
        raise ConfigEntryNotReady(f"failed to connect to {hostname}") from e
    htp1.start()
    await htp1.ready.wait()


    media_player = Htp1MediaPlayer(htp1=htp1)
    _LOGGER.debug(
        "async_setup_entry: media player created host=%s, name=%s", hostname, name
    )

    async_add_entities([media_player])
