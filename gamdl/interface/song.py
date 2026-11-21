import asyncio
import datetime
import json
import re
import sys
from typing import AsyncGenerator, Callable
from urllib.parse import unquote
from xml.dom import minidom
from xml.etree import ElementTree

import m3u8
import structlog

from .base import AppleMusicBaseInterface
from .constants import DRM_DEFAULT_KEY_MAPPING, MP4_FORMAT_CODECS, SONG_CODEC_REGEX_MAP
from .enums import SongCodec, SyncedLyricsFormat
from .exceptions import (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import (
    _BUSTED_RE,
    _FEATURED_RE,
    _FROM_RE,
    _MIXED_RE,
    _REMIX_RE,
    AppleMusicMedia,
    DecryptionKeyAv,
    Lyrics,
    MediaFileFormat,
    ParsedArtists,
    StreamInfo,
    StreamInfoAv,
)

from gamdl import __version__

sys.stdout.reconfigure(encoding='utf-8')

logger = structlog.get_logger(__name__)


class AppleMusicSongInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
        codec_priority: list[SongCodec] = [SongCodec.AAC_WEB],
        use_album_date: bool = False,
        skip_stream_info: bool = False,
        ask_codec_function: Callable[[list[dict]], dict | None] | None = None,
    ):
        self.base = base
        self.synced_lyrics_format = synced_lyrics_format
        self.codec_priority = codec_priority
        self.use_album_date = use_album_date
        self.skip_stream_info = skip_stream_info
        self.ask_codec_function = ask_codec_function

    async def get_lyrics(
        self,
        song_metadata: dict,
    ) -> Lyrics | None:
        log = logger.bind(
            action="get_lyrics",
            song_id=song_metadata["id"],
        )

        if song_metadata["attributes"]["playParams"].get("isLibrary"):
            log.debug("library_song_no_lyrics")
            return None

        if not song_metadata["attributes"]["hasLyrics"]:
            log.debug("no_lyrics")
            return None

        if (
            "relationships" not in song_metadata
            or "lyrics" not in song_metadata["relationships"]
        ):
            song_metadata = (
                await self.base.apple_music_api.get_song(
                    song_metadata["id"],
                )
            )["data"][0]

        if (
            "lyrics" in song_metadata["relationships"]
            and "data" in song_metadata["relationships"]["lyrics"]
            and len(song_metadata["relationships"]["lyrics"]["data"]) > 0
            and "attributes" in song_metadata["relationships"]["lyrics"]["data"][0]
            and song_metadata["relationships"]["lyrics"]["data"][0]["attributes"].get(
                "ttml"
            )
            is not None
        ):
            lyrics = self._get_lyrics(
                song_metadata["relationships"]["lyrics"]["data"][0]["attributes"][
                    "ttml"
                ],
            )

            log.debug("success", lyrics=lyrics)

            return lyrics
        else:
            log.debug("no_lyrics_data")

    def _get_lyrics(self, lyrics_ttml: str) -> Lyrics:
        try:
            lyrics_ttml_et = ElementTree.fromstring(lyrics_ttml)
        except ElementTree.ParseError as e:
            raise ValueError(f"Invalid TTML lyrics: {e}") from e
    
        unsynced_lyrics: list[list[str]] = []
        synced_lyrics: list[str] = []
        index = 1
        lyricist = ""
    
        for meta in lyrics_ttml_et.findall(
            ".//{http://music.apple.com/lyric-ttml-internal}iTunesMetadata"
        ):
            writers = meta.findall(
                ".//{http://music.apple.com/lyric-ttml-internal}songwriter"
            )
            if writers:
                lyricist = ", ".join(
                    w.text for w in writers if w.text
                ).strip()
    
        if self.synced_lyrics_format == SyncedLyricsFormat.TTML:
            return Lyrics(
                synced=lyrics_ttml,
                unsynced=None,
                lyricist=lyricist or None,
            )
    
        for div in lyrics_ttml_et.iter("{http://www.w3.org/ns/ttml}div"):
            stanza: list[str] = []
    
            for p in div.iter("{http://www.w3.org/ns/ttml}p"):
                line_text = "".join(p.itertext()).strip()
                if not line_text:
                    continue
    
                stanza.append(line_text)
    
                if p.attrib.get("begin"):
                    if self.synced_lyrics_format == SyncedLyricsFormat.LRC:
                        synced_lyrics.append(self._get_lyrics_line_lrc(p))
                    elif self.synced_lyrics_format == SyncedLyricsFormat.SRT:
                        synced_lyrics.append(
                            self._get_lyrics_line_srt(index, p)
                        )
                    index += 1
    
            if stanza:
                unsynced_lyrics.append(stanza)
    
        return Lyrics(
            synced="\n".join(synced_lyrics) if synced_lyrics else None,
            unsynced=(
                "\n\n".join("\n".join(group) for group in unsynced_lyrics)
                if unsynced_lyrics
                else None
            ),
            lyricist=lyricist or None,
        )

    def _parse_ttml_timestamp(
        self,
        timestamp_ttml: str,
    ) -> datetime.datetime:
        mins_secs_ms = re.findall(r"\d+", timestamp_ttml)
        ms, secs, mins = 0, 0, 0

        if len(mins_secs_ms) == 2 and ":" in timestamp_ttml:
            secs, mins = int(mins_secs_ms[-1]), int(mins_secs_ms[-2])

        elif len(mins_secs_ms) == 1:
            ms = int(mins_secs_ms[-1])

        else:
            secs = float(f"{mins_secs_ms[-2]}.{mins_secs_ms[-1]}")
            if len(mins_secs_ms) > 2:
                mins = int(mins_secs_ms[-3])

        return datetime.datetime.fromtimestamp(
            (mins * 60) + secs + (ms / 1000),
            tz=datetime.timezone.utc,
        )

    def _get_lyrics_line_srt(self, index: int, element: ElementTree.Element) -> str:
        timestamp_begin_ttml = element.attrib.get("begin")
        timestamp_end_ttml = element.attrib.get("end")
        text = element.text

        timestamp_begin = self._parse_ttml_timestamp(timestamp_begin_ttml)
        timestamp_end = self._parse_ttml_timestamp(timestamp_end_ttml)

        return (
            f"{index}\n"
            f"{timestamp_begin.strftime('%H:%M:%S,%f')[:-3]} --> "
            f"{timestamp_end.strftime('%H:%M:%S,%f')[:-3]}\n"
            f"{text}\n"
        )

    def _get_lyrics_line_lrc(self, element: ElementTree.Element) -> str:
        timestamp_ttml = element.attrib.get("begin")
        text = element.text

        timestamp = self._parse_ttml_timestamp(timestamp_ttml)
        ms_new = timestamp.strftime("%f")[:-3]

        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            timestamp += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=timestamp.microsecond
            )

        return f"[{timestamp.strftime('%M:%S.%f')[:-4]}]{text}"

    def _switch_m3u8_master_url_to_default(self, m3u8_master_url: str) -> str:
        return re.sub(
            r"(P\d+)_[^/]+(\.m3u8)",
            r"\1_default\2",
            m3u8_master_url,
        )

    def _get_m3u8_from_playback(self, playback: dict) -> str | None:
        log = logger.bind(action="get_m3u8_master_url_from_playback")

        m3u8_master_url = playback["songList"][0].get("hls-playlist-url")

        if m3u8_master_url:
            m3u8_master_url = self._switch_m3u8_master_url_to_default(m3u8_master_url)
            log.debug("success", m3u8_master_url=m3u8_master_url)
            return m3u8_master_url

        log.debug("no_m3u8_master_url")

    async def _get_m3u8_master_url_from_assets(
        self,
        media_id: str,
    ) -> str | None:
        log = logger.bind(
            action="get_m3u8_master_url_from_assets",
            song_id=media_id,
        )

        assets = await self.base.apple_music_api.get_assets(
            media_id,
            "song",
        )

        asset = next(
            (
                asset
                for asset in assets.get("results", {}).get("assets", [])
                if asset.get("url")
            ),
            None,
        )
        enhanced = asset["url"] if asset else None

        if enhanced:
            enhanced = self._switch_m3u8_master_url_to_default(enhanced)
            log.debug("success", m3u8_master_url=enhanced)
            return enhanced

        log.debug("no_m3u8_master_url")

        return None

    async def _get_m3u8_master_url(
        self,
        media_id: str,
        playback: dict | None,
    ) -> str | None:
        if playback:
            m3u8_master_url = self._get_m3u8_from_playback(playback)
            if m3u8_master_url:
                return m3u8_master_url

        return await self._get_m3u8_master_url_from_assets(media_id)

    async def get_stream_info(
        self,
        media_id: str,
        is_library: bool,
        webplayback: dict | None = None,
        playback: dict | None = None,
    ) -> StreamInfoAv:
        stream_info = None

        if is_library:
            stream_info = await self._get_library_stream_info(webplayback)
        else:
            m3u8_master_url = None
            fetched_m3u8_master_url = False

            for codec in self.codec_priority:
                if codec.is_web:
                    stream_info = await self._get_web_stream_info(webplayback, codec)
                else:
                    if not fetched_m3u8_master_url:
                        m3u8_master_url = await self._get_m3u8_master_url(
                            media_id,
                            playback,
                        )
                        fetched_m3u8_master_url = True

                    stream_info = await self._get_stream_info_nonweb(
                        m3u8_master_url,
                        codec,
                    )

                if stream_info:
                    break

        if not stream_info:
            raise GamdlInterfaceFormatNotAvailableError(
                media_id=media_id,
                codec=[codec.value for codec in self.codec_priority],
            )

        return stream_info

    async def _get_stream_info_nonweb(
        self,
        m3u8_master_url: str | None,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info")

        if not m3u8_master_url:
            log.debug("no_m3u8_master_url")
            return None

        m3u8_master_obj = m3u8.loads(
            (await self.base.get_response(m3u8_master_url)).text
        )
        m3u8_master_data = m3u8_master_obj.data
        is_enhanced = self._is_enhanced_m3u8_master(m3u8_master_data)

        if is_enhanced:
            stream_info = await self._get_stream_info_enhanced(
                m3u8_master_url,
                m3u8_master_data,
                codec,
            )
        else:
            stream_info = await self._get_stream_info_nonenhanced(
                m3u8_master_url,
                m3u8_master_data,
                codec,
            )

        if stream_info:
            log.debug(
                "success",
                stream_info=stream_info,
                is_enhanced=is_enhanced,
            )

        return stream_info

    def _is_enhanced_m3u8_master(self, m3u8_master_data: dict) -> bool:
        return any(
            playlist.get("stream_info", {}).get("audio")
            for playlist in m3u8_master_data.get("playlists", [])
        )

    async def _get_stream_info_enhanced(
        self,
        m3u8_master_url: str,
        m3u8_master_data: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info_enhanced")

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec_enhanced(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            log.debug("no_matching_playlist", codec=codec.value)
            return None

        stream_info = await self._get_stream_info_from_playlist(
            m3u8_master_url,
            playlist,
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    async def _get_stream_info_nonenhanced(
        self,
        m3u8_master_url: str,
        m3u8_master_data: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info_nonenhanced")

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec_nonenhanced(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            log.debug("no_matching_playlist", codec=codec.value)
            return None

        stream_info = await self._get_stream_info_from_playlist(
            m3u8_master_url,
            playlist,
            True,
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    async def _get_stream_info_from_playlist(
        self,
        m3u8_master_url: str,
        playlist: dict,
        use_single_content_key: bool = False,
    ) -> StreamInfoAv:
        stream_info = StreamInfo(use_single_content_key=use_single_content_key)
        stream_info.stream_url = (
            f"{m3u8_master_url.rpartition('/')[0]}/{playlist['uri']}"
        )
        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(stream_info.codec.startswith(codec) for codec in MP4_FORMAT_CODECS)

        m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )

        stream_info.widevine_pssh = self._get_drm_uri_from_m3u8_keys(
            m3u8_obj,
            "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
        )
        stream_info.playready_pssh = self._get_drm_uri_from_m3u8_keys(
            m3u8_obj,
            "com.microsoft.playready",
        )
        stream_info.fairplay_key = self._get_drm_uri_from_m3u8_keys(
            m3u8_obj,
            "com.apple.streamingkeydelivery",
        )

        stream_info_av = StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.MP4 if is_mp4 else MediaFileFormat.M4A,
        )

        return stream_info_av

    def _get_playlist_from_codec_enhanced(
        self, m3u8_data: dict, codec: SongCodec
    ) -> dict | None:
        matching_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if re.fullmatch(
                SONG_CODEC_REGEX_MAP[codec.value], playlist["stream_info"]["audio"]
            )
        ]

        if not matching_playlists:
            return None

        return max(
            matching_playlists,
            key=lambda x: x["stream_info"]["average_bandwidth"],
        )

    def _get_playlist_from_codec_nonenhanced(
        self, m3u8_data: dict, codec: SongCodec
    ) -> dict | None:
        codec_values = {
            SongCodec.AAC: {"mp4a.40.2"},
            SongCodec.AAC_HE: {"mp4a.40.5"},
        }.get(codec)
        if not codec_values:
            return None

        matching_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if playlist["stream_info"].get("codecs") in codec_values
        ]

        if not matching_playlists:
            return None

        return max(
            matching_playlists,
            key=lambda x: x["stream_info"]["average_bandwidth"],
        )

    async def _get_playlist_from_user(self, m3u8_data: dict) -> dict | None:
        if self.ask_codec_function:
            playlist = self.ask_codec_function(
                [playlist for playlist in m3u8_data["playlists"]]
            )
            if asyncio.iscoroutine(playlist):
                playlist = await playlist

            return playlist

        return None

    def _get_drm_uri_from_m3u8_keys(
        self,
        m3u8_obj: m3u8.M3U8,
        drm_key: str,
    ) -> str | None:
        default_uri = DRM_DEFAULT_KEY_MAPPING[drm_key]

        for key in m3u8_obj.keys:
            if key.keyformat == drm_key and key.uri != default_uri:
                return key.uri
        return None

    async def _get_web_stream_info(
        self,
        webplayback: dict | None,
        codec: SongCodec,
    ) -> StreamInfoAv:
        log = logger.bind(action="get_web_song_stream_info")

        if not webplayback:
            log.debug("no_webplayback")
            return None

        flavor = codec.flavor

        stream_info = StreamInfo(
            use_cenc=codec.is_cenc,
        )
        asset = next(
            (i for i in webplayback["songList"][0]["assets"] if i["flavor"] == flavor),
            None,
        )
        if not asset:
            log.debug("no_matching_asset", codec=codec.value, flavor=flavor)
            return None

        stream_info.stream_url = asset["URL"]

        m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )

        if stream_info.use_cenc:
            stream_info.widevine_pssh = m3u8_obj.keys[0].uri
        else:
            stream_info.fairplay_key = m3u8_obj.keys[0].uri

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    async def _get_library_stream_info(
        self,
        webplayback: dict | None,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_library_song_stream_info")

        if not webplayback:
            log.debug("no_webplayback")
            return None

        stream_info = StreamInfo(drm_free=True)

        if len(webplayback["songList"][0]["assets"]) == 0:
            log.debug("no_matching_asset")
            return None
        asset = webplayback["songList"][0]["assets"][0]

        stream_info.stream_url = asset["URL"]

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    def split_artist_string(self, artist_str: str) -> list[str]:
        """
        Разбивает строку артистов на список.
        Обрабатывает: запятые, &, 'feat.', 'and'
        
        'Artist1, Artist2 & Artist3' → ['Artist1', 'Artist2', 'Artist3']
        """
        if not artist_str:
            return []
    
        # Заменяем & и 'and' на запятые (но не внутри скобок)
        normalized = re.sub(r'\s*&\s*', ', ', artist_str)
        normalized = re.sub(r'\s+and\s+', ', ', normalized, flags=re.IGNORECASE)
    
        # Разбиваем по запятым
        parts = [p.strip() for p in normalized.split(',')]
    
        # Убираем пустые
        return [p for p in parts if p]
    
    def parse_track_artists(
        self,
        main_artists: list[str],
        track_name: str,
    ) -> ParsedArtists:
        """
        Парсит заголовок трека для извлечения featured, remixers и пр.
        
        Args:
            main_artists: Список основных артистов из AMP API
            track_name: Полное название трека (например, 
                        "Song (feat. X) [Y Remix]")
        
        Returns:
            ParsedArtists с разложенными по категориям артистами
        """
        result = ParsedArtists(main=list(main_artists))
    
        if not track_name:
            return result
    
        remaining = track_name
    
        # 1. Извлекаем featured
        for match in _FEATURED_RE.finditer(remaining):
            featured_str = match.group(1).strip()
            result.featured.extend(self.split_artist_string(featured_str))
        remaining = _FEATURED_RE.sub('', remaining)
    
        # 2. Извлекаем ремиксеров (ПОСЛЕ featured, чтобы не перепутать)
        for match in _REMIX_RE.finditer(remaining):
            remixer_str = match.group(1).strip()
            result.remixers.extend(self.split_artist_string(remixer_str))
        remaining = _REMIX_RE.sub('', remaining)

        for match in _BUSTED_RE.finditer(remaining):
            remixer_str = match.group(1).strip()
            result.remixers.extend(self.split_artist_string(remixer_str))
        remaining = _BUSTED_RE.sub('', remaining)
    
        # 3. Проверяем (Mixed)
        if _MIXED_RE.search(remaining):
            result.extras.append("Mixed")
            remaining = _MIXED_RE.sub('', remaining)
    
        # 4. Извлекаем "From ..." (саундтреки)
        for match in _FROM_RE.finditer(remaining):
            result.extras.append(f"From: {match.group(1).strip()}")
        remaining = _FROM_RE.sub('', remaining)
    
        # Убираем дубликаты, сохраняя порядок
        result.main = list(dict.fromkeys(result.main))
        result.featured = list(dict.fromkeys(result.featured))
        result.remixers = list(dict.fromkeys(result.remixers))
    
        # Убираем из featured тех, кто уже в main
        main_set = {a.lower() for a in result.main}
        result.featured = [f for f in result.featured if f.lower() not in main_set]
    
        # Убираем из remixers тех, кто уже в main или featured
        featured_set = {a.lower() for a in result.featured}
        result.remixers = [
            r for r in result.remixers
            if r.lower() not in main_set and r.lower() not in featured_set
        ]
    
        return result

    async def get_media(
        self,
        media: AppleMusicMedia,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        if not media.media_metadata:
            media.media_metadata = (
                await (
                    self.base.apple_music_api.get_library_song(media.media_id)
                    if media.is_library
                    else self.base.apple_music_api.get_full_song(media.media_id)
                )
            )["data"][0]

        if media.media_metadata["attributes"].get("playParams", {}).get("isLibrary"):
            catalog_metadata = self.base.get_catalog_metadata_from_library(
                media.media_metadata
            )
            if catalog_metadata:
                media.media_id = catalog_metadata["id"]
                media.is_library = False
                media.media_metadata = catalog_metadata

        yield media

        if not self.base.is_media_streamable(media.media_metadata):
            raise GamdlInterfaceMediaNotStreamableError(
                media_id=media.media_id,
            )

        if media.playlist_metadata:
            media.playlist_tags = self.base.get_playlist_tags(
                media.playlist_metadata,
                media.index,
            )

        media.cover = await self.base.get_cover(media.media_metadata)

        media.lyrics = await self.get_lyrics(media.media_metadata)

        if self.base.wrapper_api:
            playback = (
                await self.base.wrapper_api.get_playback(media.media_id)
                if not media.is_library
                else None
            )
            webplayback = (
                await self.base.apple_music_api.get_webplayback(
                    media.media_id,
                    media.is_library,
                )
                if media.is_library
                or any(codec.is_web for codec in self.codec_priority)
                else None
            )
        else:
            playback = None
            webplayback = await self.base.apple_music_api.get_webplayback(
                media.media_id,
                media.is_library,
            )

        lyrics_text = (
            media.lyrics.synced or media.lyrics.unsynced
            if media.lyrics
            else None
        )

        if playback:
            metadata = playback["songList"][0]["assets"][0]["metadata"]
        else:
            metadata = webplayback["songList"][0]["assets"][0]["metadata"]

        media.tags = await self.base.get_tags_from_asset_info(
            metadata,
            lyrics_text,
            self.use_album_date,
        )

        if not self.skip_stream_info:
            media.stream_info = await self.get_stream_info(
                media.media_id,
                media.is_library,
                webplayback,
                playback,
            )

            if media.stream_info.audio_track.drm_free:
                pass
            elif (
                not self.base.wrapper_api
                and not media.stream_info.audio_track.widevine_pssh
            ) or (
                self.base.wrapper_api
                and not media.stream_info.audio_track.fairplay_key
                and not media.stream_info.audio_track.use_cenc
            ):
                raise GamdlInterfaceDecryptionNotAvailableError(media_id=media.media_id)
            elif media.stream_info.audio_track.widevine_pssh:
                media.decryption_key = DecryptionKeyAv(
                    audio_track=await self.base.get_decryption_key(
                        media.stream_info.audio_track.widevine_pssh,
                        media.media_id,
                    )
                )

        media.partial = False

        # print(json.dumps(media.media_metadata, indent=2, ensure_ascii=False))

        media.tags.www = unquote(media.media_metadata["attributes"]["url"])
        media.tags.isrc = media.media_metadata["attributes"]["isrc"]
        media.tags.label = media.media_metadata["relationships"]["albums"]["data"][0]["attributes"].get("recordLabel", "")
        media.tags.upc = media.media_metadata["relationships"]["albums"]["data"][0]["attributes"].get("upc", "")

        media.tags.lyricist = getattr(media.lyrics, 'lyricist', '') or None

        media.tags.encoding_tool = f'gamdl {__version__}'
        media.tags.encoder = f'gamdl'

        attrs = media.media_metadata["attributes"]
        for key in ["movementNumber", "movementCount", "movementName", "workName"]:
            if value := attrs.get(key):
                setattr(media.tags, key, value)

        media.tags.artist = media.media_metadata.get("artistNames", media.tags.artist)
        if media.media_metadata.get("albumArtistNames", []):
            media.tags.album_artist = media.media_metadata.get("albumArtistNames", media.tags.album_artist)

        parsed = self.parse_track_artists(
            main_artists=media.tags.artist,
            track_name=media.tags.title,
        )

        # Формируем полный список артистов в правильном порядке:
        # 1. Основные → 2. Featured → 3. Ремиксеры
        full_artist_list = parsed.main + parsed.featured + parsed.remixers

        # Перезаписываем теги
        media.tags.artist = full_artist_list if full_artist_list else media.tags.artist
        media.tags.featured_artists = parsed.featured if parsed.featured else media.tags.featured_artists
        media.tags.remixed_artists = parsed.remixers if parsed.remixers else media.tags.remixed_artists

        # Сохраняем разбор в кастомные атомы (опционально)
        # media.tags._parsed_artists = parsed  # если нужно использовать потом

        yield media
