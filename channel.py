from __future__ import annotations

import re
import gzip
import json
import asyncio
import logging
from base64 import b64encode
from collections import OrderedDict
from functools import cached_property
from time import monotonic
from typing import Any, SupportsInt, TYPE_CHECKING

import aiohttp
from yarl import URL

from utils import Game, json_minify, isonow
from exceptions import ExitRequest, MinerException, ReloadRequest, RequestException
from constants import CALL, GQL_QUERIES, ONLINE_DELAY, WATCH_INTERVAL, URLType, GQLQuery

if TYPE_CHECKING:
    from twitch import Twitch
    from gui import ChannelList
    from constants import JsonType, GQLPersistedQuery


logger = logging.getLogger("TwitchDrops")


class Stream:
    def __init__(
        self,
        channel: Channel,
        *,
        id: SupportsInt,
        game: JsonType | None,
        viewers: int,
        title: str,
    ):
        self.channel: Channel = channel
        self.broadcast_id = int(id)
        self.viewers: int = viewers
        self.drops_enabled: bool = not channel._twitch.settings.available_drops_check
        self.game: Game | None = Game(game) if game else None
        self.title: str = title
        self._stream_url: URLType | None = None

    @cached_property
    def _watch_payload(self) -> list[JsonType]:
        return [
            {
                "event": "minute-watched",
                "properties": {
                    "broadcast_id": str(self.broadcast_id),
                    "channel_id": str(self.channel.id),
                    "channel": self.channel._login,
                    "client_time": isonow(),
                    "game": self.game.name if self.game is not None else "",
                    "game_id": str(self.game.id) if self.game is not None else "",
                    "hidden": False,
                    "is_live": True,
                    "live": True,
                    "logged_in": True,
                    "minutes_logged": 1,
                    "muted": False,
                    "user_id": self.channel._twitch._auth_state.user_id,
                }
            }
        ]

    @cached_property
    def spade_payload(self) -> JsonType:
        return {
            "data": (b64encode(json_minify(self._watch_payload).encode("utf8"))).decode("utf8")
        }

    @cached_property
    def gql_payload(self) -> GQLQuery:
        return GQLQuery(
            (
                "\n mutation SendEvents($input: SendSpadeEventsInput!) "
                "{\n sendSpadeEvents(input: $input) {\n statusCode\n}\n}\n"
            ),
            b64encode(
                gzip.compress(json_minify(self._watch_payload).encode("utf8"))
            ).decode("utf8")
        )

    @classmethod
    def from_get_stream(cls, channel: Channel, channel_data: JsonType) -> Stream:
        stream = channel_data["stream"]
        settings = channel_data["broadcastSettings"]
        result = cls(
            channel,
            id=stream["id"],
            game=settings["game"],
            viewers=stream["viewersCount"],
            title=settings["title"],
        )
        if channel._stream is not None and result == channel._stream:
            result._stream_url = channel._stream._stream_url
        return result

    @classmethod
    def from_directory(
        cls, channel: Channel, channel_data: JsonType, *, drops_enabled: bool = False
    ) -> Stream:
        self = cls(
            channel,
            id=channel_data["id"],
            game=channel_data["game"],  # has to be there since we searched with it
            viewers=channel_data["viewersCount"],
            title=channel_data["title"],
        )
        self.drops_enabled = drops_enabled
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, self.__class__):
            return self.broadcast_id == other.broadcast_id
        return NotImplemented

    async def get_stream_url(self) -> URLType | None:
        if self._stream_url is not None:
            return self._stream_url
        # get the stream playback access token from GQL
        playback_token_response: JsonType = await self.channel._twitch.gql_request(
            GQL_QUERIES["PlaybackAccessToken"].with_variables({"login": self.channel._login})
        )
        data = playback_token_response.get("data")
        token_data = data.get("streamPlaybackAccessToken") if isinstance(data, dict) else None
        if not isinstance(token_data, dict) or not all(
            isinstance(token_data.get(key), str) and token_data[key]
            for key in ("value", "signature")
        ):
            return None
        master_url = URL(
            f"https://usher.ttvnw.net/api/channel/hls/{self.channel._login}.m3u8"
        ).with_query(sig=token_data["signature"], token=token_data["value"])
        async with self.channel._twitch.request("GET", master_url) as response:
            if response.status != 200:
                return None
            master = await response.text()
        qualities = self.playlist_urls(master, master_url, master=True)
        if not qualities:
            return None
        # The last quality is normally audio-only or the lowest bandwidth variant.
        self._stream_url = URLType(str(qualities[-1]))
        return self._stream_url

    @staticmethod
    def playlist_urls(playlist: str, base: URL, *, master: bool = False) -> list[URL]:
        """Resolve URI lines associated with the expected HLS entry tag."""
        if not playlist.lstrip().startswith("#EXTM3U"):
            return []
        tag = "#EXT-X-STREAM-INF:" if master else "#EXTINF:"
        urls: list[URL] = []
        pending = False
        try:
            for raw in playlist.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if line.startswith(tag):
                        pending = True
                    continue
                if not pending:
                    return []
                url = base.join(URL(line))
                if (
                    url.scheme not in ("http", "https") or not url.host
                    or url.user is not None or master and not url.path.endswith(".m3u8")
                ):
                    return []
                urls.append(url)
                pending = False
            return [] if pending else urls
        except (ValueError, aiohttp.InvalidURL):
            return []


class Channel:
    __slots__ = (
        "_twitch", "_gui_channels", "id", "_login", "_display_name", "_spade_url",
        "_stream", "_pending_stream_up", "acl_based",
        "_watch_broadcast_id", "_watched_segments", "_last_spade_sent",
    )

    def __init__(
        self,
        twitch: Twitch,
        *,
        id: SupportsInt,
        login: str,
        display_name: str | None = None,
        acl_based: bool = False,
    ):
        self._twitch: Twitch = twitch
        self._gui_channels: ChannelList = twitch.gui.channels
        self.id: int = int(id)
        self._login: str = login
        self._display_name: str | None = display_name
        self._spade_url: URLType | None = None
        self._stream: Stream | None = None
        self._pending_stream_up: asyncio.Task[Any] | None = None
        self._watch_broadcast_id: int | None = None
        self._watched_segments: OrderedDict[str, None] = OrderedDict()
        self._last_spade_sent: float | None = None
        # ACL-based channels are:
        # • considered first when switching channels
        # • if we're watching a non-based channel, a based channel going up triggers a switch
        # • not cleaned up unless they're streaming a game we haven't selected
        self.acl_based: bool = acl_based

    @classmethod
    def from_acl(cls, twitch: Twitch, data: JsonType) -> Channel:
        return cls(
            twitch,
            id=data["id"],
            login=data["name"],
            display_name=data.get("displayName"),
            acl_based=True,
        )

    @classmethod
    def from_directory(
        cls, twitch: Twitch, data: JsonType, *, drops_enabled: bool = False
    ) -> Channel:
        channel = data["broadcaster"]
        self = cls(
            twitch, id=channel["id"], login=channel["login"], display_name=channel["displayName"]
        )
        self._stream = Stream.from_directory(self, data, drops_enabled=drops_enabled)
        return self

    def __repr__(self) -> str:
        if self._display_name is not None:
            name = f"{self._display_name}({self._login})"
        else:
            name = self._login
        return f"Channel({name}, {self.id})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, self.__class__):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return self.id

    @property
    def stream_gql(self) -> GQLPersistedQuery:
        return GQL_QUERIES["GetStreamInfo"].with_variables({"channel": self._login})

    @property
    def name(self) -> str:
        if self._display_name is not None:
            return self._display_name
        return self._login

    @property
    def url(self) -> URLType:
        # Channel HTML is always fetched from the normal Twitch website. SMARTBOX
        # authentication is only an API identity; android.tv.twitch.tv channel pages
        # do not expose the settings/spade metadata needed for watch tracking.
        return URLType(f"https://www.twitch.tv/{self._login}")

    @property
    def iid(self) -> str:
        """
        Returns a string to be used as ID/key of the columns inside channel list.
        """
        return str(self.id)

    @property
    def online(self) -> bool:
        """
        Returns True if the streamer is online and is currently streaming, False otherwise.
        """
        return self._stream is not None

    @property
    def offline(self) -> bool:
        """
        Returns True if the streamer is offline and isn't about to come online, False otherwise.
        """
        return self._stream is None and self._pending_stream_up is None

    @property
    def pending_online(self) -> bool:
        """
        Returns True if the streamer is about to go online (most likely), False otherwise.
        This is because 'stream-up' event is received way before
        stream information becomes available.
        """
        return self._stream is None and self._pending_stream_up is not None

    @property
    def game(self) -> Game | None:
        if self._stream is not None:
            return self._stream.game
        return None

    @property
    def viewers(self) -> int | None:
        if self._stream is not None:
            return self._stream.viewers
        return None

    @viewers.setter
    def viewers(self, value: int):
        if self._stream is not None:
            self._stream.viewers = value

    @property
    def drops_enabled(self) -> bool:
        if self._stream is not None:
            return self._stream.drops_enabled
        return False

    def display(self, *, add: bool = False):
        self._gui_channels.display(self, add=add)

    def remove(self):
        if self._pending_stream_up is not None:
            self._pending_stream_up.cancel()
            self._pending_stream_up = None
        self._gui_channels.remove(self)

    async def get_spade_url(self) -> URLType:
        """
        To get this monstrous thing, you have to walk a chain of requests.
        Streamer page (HTML) --parse-> Streamer Settings (JavaScript) --parse-> Spade URL

        For mobile view, spade_url is available immediately from the page, skipping step #2.
        """
        SETTINGS_PATTERN: str = r'src="(https://[\w.]+/config/settings\.[0-9a-f]{32}\.js)"'
        SPADE_PATTERN: str = r'"spade_?url": ?"(https://[.\w\-/]+)"'
        async with self._twitch.request("GET", self.url) as response1:
            streamer_html: str = await response1.text(encoding="utf8")
        match = re.search(SPADE_PATTERN, streamer_html, re.I)
        if not match:
            match = re.search(SETTINGS_PATTERN, streamer_html, re.I)
            if not match:
                raise MinerException("Error while spade_url extraction: step #1")
            streamer_settings = match.group(1)
            async with self._twitch.request("GET", streamer_settings) as response2:
                settings_js: str = await response2.text(encoding="utf8")
            match = re.search(SPADE_PATTERN, settings_js, re.I)
            if not match:
                raise MinerException("Error while spade_url extraction: step #2")
        spade_url = URLType(match.group(1))
        # FIX perso 2026-10-05: spade.twitch.tv est sinkholé en 0.0.0.0 par
        # certains DNS/adblockers -> connexion impossible en boucle.
        # beacon.twitch.tv = même edge analytics Twitch et répond.
        if "spade.twitch.tv" in spade_url:
            spade_url = URLType(spade_url.replace("spade.twitch.tv", "beacon.twitch.tv"))
        return spade_url

    def _check_drops_enabled(self, available_drops: list[JsonType]) -> bool:
        return any(
            (
                (campaign := self._twitch._campaigns.get(campaign_data["id"])) is not None
                and campaign.can_earn(self, ignore_channel_status=True)
            )
            for campaign_data in available_drops
        )

    def update_available_drops(self, available_drops: list[JsonType]) -> None:
        if self._stream is not None:
            self._stream.drops_enabled = self._check_drops_enabled(available_drops)

    def external_update(self, channel_data: JsonType, available_drops: list[JsonType]):
        """
        Update stream information based on data provided externally.

        Used for bulk-updates of channel statuses during reload.
        """
        if not channel_data["stream"]:
            self._stream = None
            return
        stream = Stream.from_get_stream(self, channel_data)
        if not stream.drops_enabled:
            stream.drops_enabled = self._check_drops_enabled(available_drops)
        self._stream = stream

    async def get_stream(self) -> Stream | None:
        try:
            response: JsonType = await self._twitch.gql_request(self.stream_gql)
        except MinerException as exc:
            raise MinerException(f"Channel: {self._login}") from exc
        channel_data: JsonType | None = response["data"]["user"]
        if not channel_data:
            return None
        # fill in display name
        if self._display_name is None:
            self._display_name = channel_data["displayName"]
        if not channel_data["stream"]:
            return None
        stream = Stream.from_get_stream(self, channel_data)
        if not stream.drops_enabled:
            try:
                available_drops_campaigns: JsonType = await self._twitch.gql_request(
                    GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(self.id)})
                )
            except MinerException:
                logger.log(CALL, f"AvailableDrops GQL call failed for channel: {self._login}")
            else:
                stream.drops_enabled = self._check_drops_enabled(
                    available_drops_campaigns["data"]["channel"]["viewerDropCampaigns"] or []
                )
        return stream

    async def update_stream(self) -> bool:
        """
        Fetches the current channel stream, and if one exists,
        updates it's game, title, tags and viewers. Updates channel status in general.
        """
        old_stream = self._stream
        self._stream = await self.get_stream()
        self._twitch.on_channel_update(self, old_stream, self._stream)
        return self._stream is not None

    async def _online_delay(self):
        """
        The 'stream-up' event is sent before the stream actually goes online,
        so just wait a bit and check if it's actually online by then.
        """
        await asyncio.sleep(ONLINE_DELAY.total_seconds())
        self._pending_stream_up = None  # for 'display' to work properly
        await self.update_stream()

    def check_online(self):
        """
        Sets up a task that will wait ONLINE_DELAY duration,
        and then check for the stream being ONLINE OR OFFLINE.

        If the channel is OFFLINE, it sets the channel's status to PENDING_ONLINE,
        where after ONLINE_DELAY, it's going to be set to ONLINE.
        If the channel is ONLINE already, after ONLINE_DELAY,
        it's status is going to be double-checked to ensure it's actually ONLINE.

        This is called externally, if we receive an event about the status possibly being ONLINE
        or having to be updated.
        """
        if self._pending_stream_up is None:
            self._pending_stream_up = asyncio.create_task(self._online_delay())
            self.display()

    def set_offline(self):
        """
        Sets the channel status to OFFLINE. Cancels PENDING_ONLINE if applicable.

        This is called externally, if we receive an event indicating the channel is now OFFLINE.
        """
        needs_display: bool = False
        if self._pending_stream_up is not None:
            self._pending_stream_up.cancel()
            self._pending_stream_up = None
            needs_display = True
        if self.online:
            old_stream = self._stream
            self._stream = None
            self._twitch.on_channel_update(self, old_stream, self._stream)
            needs_display = False  # calling on_channel_update always does a display at the end
        if needs_display:
            self.display()

    def _watch_current(self, stream: Stream) -> bool:
        return (
            self._stream is not None
            and self._stream.broadcast_id == stream.broadcast_id
            and self._twitch.watching_channel.get_with_default(None) is self
        )

    async def _send_watch_playlist(self) -> bool:
        """HEAD every new media segment, without fetching audio or video bodies."""
        try:
            # Twitch.request retries network/5xx failures for minutes. Bound the entire poll
            # as well as individual requests so one missing segment cannot stall watching.
            return await asyncio.wait_for(self._watch_playlist(), timeout=10)
        except (ExitRequest, ReloadRequest):
            raise
        except (MinerException, aiohttp.ClientError, TimeoutError, ValueError):
            return False

    async def _watch_playlist(self) -> bool:
        stream = self._stream
        if stream is None or not self._watch_current(stream):
            return False
        if self._watch_broadcast_id != stream.broadcast_id:
            self._watch_broadcast_id = stream.broadcast_id
            self._watched_segments.clear()
            self._last_spade_sent = None
        stream_url = await stream.get_stream_url()
        if stream_url is None or not self._watch_current(stream):
            return False
        status, playlist = await asyncio.wait_for(
            self._watch_response("GET", stream_url), timeout=5
        )
        if status != 200:
            if status in (401, 403, 404):
                stream._stream_url = None
                if self._stream is not None and self._stream == stream:
                    self._stream._stream_url = None
            return False
        if not self._watch_current(stream):
            return False
        segments = Stream.playlist_urls(playlist, URL(stream_url))
        if not segments:
            return False
        succeeded = True
        for url in segments:
            if not self._watch_current(stream):
                return False
            key = str(url)
            if key in self._watched_segments:
                continue
            try:
                status, _ = await asyncio.wait_for(
                    self._watch_response("HEAD", url), timeout=3
                )
                if not self._watch_current(stream):
                    return False
                if status == 200:
                    self._watched_segments[key] = None
                    # Retain some history for temporarily stale CDN snapshots,
                    # while keeping memory bounded during long broadcasts.
                    if len(self._watched_segments) > 256:
                        self._watched_segments.popitem(last=False)
                else:
                    succeeded = False
                    if status in (401, 403, 404):
                        stream._stream_url = None
            except (ExitRequest, ReloadRequest):
                raise
            except (MinerException, aiohttp.ClientError, TimeoutError):
                succeeded = False
        return succeeded

    async def _watch_response(self, method: str, url: URLType | URL) -> tuple[int, str]:
        # The CDN forcibly disconnects shortly after serving the playlist.
        headers = {"Connection": "close"} if method == "GET" else {}
        async with self._twitch.request(method, url, headers=headers) as response:
            return response.status, await response.text() if method == "GET" else ""

    async def send_watch(self) -> bool:
        stream = self._stream
        if stream is None:
            return False
        succeeded = await self._send_watch_playlist()
        if not succeeded or not self._watch_current(stream):
            return False
        now = monotonic()
        # Telemetry is auxiliary; its response does not establish Drop credit.
        if (
            self._last_spade_sent is None
            or now - self._last_spade_sent >= WATCH_INTERVAL.total_seconds()
        ):
            self._last_spade_sent = now
            await self._send_watch_spade()
        return self._watch_current(stream)

    async def _send_watch_spade(self) -> bool:
        try:
            return await asyncio.wait_for(self._post_spade(), timeout=5)
        except (ExitRequest, ReloadRequest):
            raise
        except (MinerException, aiohttp.ClientError, TimeoutError):
            return False

    async def _post_spade(self) -> bool:
        stream = self._stream
        if stream is None:
            return False
        if self._spade_url is None:
            try:
                self._spade_url = await self.get_spade_url()
            except MinerException as exc:
                # FIX perso: une extraction spade ratée (page changée, host
                # bloqué...) ne doit pas tuer l'appli : on saute ce cycle,
                # la prochaine tentative réessaiera l'extraction.
                logger.warning(f"Spade URL extraction failed for {self._login}: {exc}")
                return False
        if not self._watch_current(stream):
            return False
        async with self._twitch.request(
            "POST", self._spade_url, data=stream.spade_payload
        ) as response:
            return response.status == 204

    # NOTE: This is currently unused.
    async def _send_watch_gql(self) -> bool:
        if self._stream is None:
            return False
        try:
            watch_response = await self._twitch.gql_request(self._stream.gql_payload)
            return watch_response["data"]["sendSpadeEvents"]["statusCode"] == 204
        except RequestException:
            return False
