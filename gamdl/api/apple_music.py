import json
import re
from http.cookiejar import MozillaCookieJar
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from httpx_retries import Retry, RetryTransport

from .constants import (
    APPLE_MUSIC_ACCOUNT_INFO_API_URI,
    APPLE_MUSIC_ALBUM_API_URI,
    APPLE_MUSIC_AMP_API_URL,
    APPLE_MUSIC_ARTIST_API_URI,
    APPLE_MUSIC_ASSETS_API_URI,
    APPLE_MUSIC_COOKIE_DOMAIN,
    APPLE_MUSIC_HOMEPAGE_URL,
    APPLE_MUSIC_LIBRARY_ALBUM_API_URI,
    APPLE_MUSIC_LIBRARY_PLAYLIST_API_URI,
    APPLE_MUSIC_LIBRARY_PLAYLISTS_API_URI,
    APPLE_MUSIC_LICENSE_API_URL,
    APPLE_MUSIC_LIBRARY_MUSIC_VIDEO_API_URI,
    APPLE_MUSIC_MUSIC_VIDEO_API_URI,
    APPLE_MUSIC_LIBRARY_ALBUMS_API_URI,
    APPLE_MUSIC_PLAYLIST_API_URI,
    APPLE_MUSIC_SEARCH_API_URI,
    APPLE_MUSIC_LIBRARY_MUSIC_VIDEOS_API_URI,
    APPLE_MUSIC_LIBRARY_SONG_API_URI,
    APPLE_MUSIC_LIBRARY_SONGS_API_URI,
    APPLE_MUSIC_SONG_API_URI,
    APPLE_MUSIC_UPLOADED_VIDEO_API_URL,
    APPLE_MUSIC_WEBPLAYBACK_API_URL,
)
from .exceptions import GamdlApiResponseError
from .wrapper import WrapperApi

logger = structlog.get_logger(__name__)

import asyncio
from async_lru import alru_cache
from typing import Optional

class AppleMusicApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        storefront: str,
        language: str,
        media_user_token: str | None = None,
        account_info: dict | None = None,
    ) -> None:
        self.token = token
        self.storefront = storefront
        self.language = language
        self.media_user_token = media_user_token
        self.account_info = account_info
        self.client = client

    @property
    def active_subscription(self) -> bool:
        if not self.account_info:
            return False

        return (
            self.account_info.get("meta", {})
            .get("subscription", {})
            .get("active", False)
        )

    @property
    def account_restrictions(self) -> dict | None:
        if not self.account_info:
            return None

        data = self.account_info.get("data", [])
        if not data:
            return None
        return data[0].get("attributes", {}).get("restrictions")

    @staticmethod
    async def get_token() -> str:
        log = logger.bind(action="get_token")

        response = None
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    APPLE_MUSIC_HOMEPAGE_URL,
                    follow_redirects=True,
                )
                response.raise_for_status()
                home_page = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching Apple Music homepage",
                    status_code=response.status_code if response is not None else None,
                )

        index_js_uri_match = re.search(
            r"/(assets/index[~-][^/\"]+\.js)",
            home_page,
        )
        if not index_js_uri_match:
            raise GamdlApiResponseError(
                "Error finding index.js URI in Apple Music homepage"
            )
        index_js_uri = index_js_uri_match.group(1)

        response = None
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                response = await client.get(
                    f"{APPLE_MUSIC_HOMEPAGE_URL}/{index_js_uri}"
                )
                response.raise_for_status()
                index_js_page = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching index.js page",
                    status_code=response.status_code if response is not None else None,
                )

        token_match = re.search(r'"(eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)"', index_js_page)
        if not token_match:
            raise GamdlApiResponseError("Error finding token in index.js page")
        token = token_match.group(1)

        log.debug("success")

        return token

    @staticmethod
    async def get_account_info(
        token: str,
        media_user_token: str,
        meta: str = "subscription",
    ) -> dict:
        log = logger.bind(action="get_account_info", meta=meta)

        response = None
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    APPLE_MUSIC_AMP_API_URL + APPLE_MUSIC_ACCOUNT_INFO_API_URI,
                    params={
                        "meta": meta,
                    },
                    headers={
                        "authorization": f"Bearer {token}",
                        "origin": APPLE_MUSIC_HOMEPAGE_URL,
                        "cookie": f"media-user-token={media_user_token}",
                    },
                )
                response.raise_for_status()
                account_info = response.json()
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching account info",
                    status_code=response.status_code if response is not None else None,
                )

        log.debug("success", account_info=account_info)

        return account_info

    @classmethod
    async def create(
        cls,
        storefront: str | None = "us",
        language: str = "en-US",
        token: str | None = None,
        media_user_token: str | None = None,
    ) -> "AppleMusicApi":
        token = token or await cls.get_token()
        account_info = (
            await cls.get_account_info(token, media_user_token)
            if media_user_token
            else None
        )
        storefront = (
            account_info["meta"]["subscription"]["storefront"]
            if account_info
            else storefront
        )
        if not storefront:
            raise ValueError(
                "Storefront must be provided if it cannot be determined from account info"
            )

        client = httpx.AsyncClient(
            headers={
                "authorization": f"Bearer {token}",
                "origin": APPLE_MUSIC_HOMEPAGE_URL,
            },
            transport=RetryTransport(
                retry=Retry(
                    total=6,
                    backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                )
            ),
        )

        if media_user_token:
            client.headers.update(
                {
                    "cookie": f"media-user-token={media_user_token}",
                }
            )

        api = cls(
            client=client,
            token=token,
            storefront=storefront,
            language=language,
            media_user_token=media_user_token,
            account_info=account_info,
        )
        return api

    @classmethod
    async def create_from_netscape_cookies(
        cls,
        cookies_path: str = "./cookies.txt",
        *args,
        **kwargs,
    ) -> "AppleMusicApi":
        cookies = MozillaCookieJar(cookies_path)
        cookies.load(ignore_discard=True, ignore_expires=True)
        parse_cookie = lambda name: next(
            (
                cookie.value
                for cookie in cookies
                if cookie.name == name and cookie.domain == APPLE_MUSIC_COOKIE_DOMAIN
            ),
            None,
        )

        media_user_token = parse_cookie("media-user-token")
        if not media_user_token:
            raise ValueError(
                '"media-user-token" cookie not found in cookies. '
                "Make sure you have exported the cookies from the Apple Music webpage "
                "and are logged in with an active subscription."
            )

        return await cls.create(
            media_user_token=media_user_token,
            *args,
            **kwargs,
        )

    @classmethod
    async def create_from_wrapper(
        cls,
        wrapper_api: WrapperApi,
        *args,
        **kwargs,
    ) -> "AppleMusicApi":
        auth = wrapper_api.me.get("auth", {})
        media_user_token = auth.get("music_user_token")
        token = auth.get("dev_token")
        if not media_user_token or not token:
            raise GamdlApiResponseError(
                "Wrapper account info is missing auth tokens",
                status_code=None,
            )

        return await cls.create(
            media_user_token=media_user_token,
            token=token,
            *args,
            **kwargs,
        )

    async def _amp_request(
        self,
        uri: str,
        params: dict | None = None,
    ) -> dict:
        response = None
        try:
            response = await self.client.get(
                APPLE_MUSIC_AMP_API_URL + uri,
                params=params,
            )
            response.raise_for_status()
            response_json = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching from AMP API",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if "errors" in response_json:
            raise GamdlApiResponseError(
                "Error fetching from AMP API",
                content=response_json["errors"],
            )

        return response_json

    async def _resolve_artist_names(
        self,
        artists_data: list[dict],
    ) -> tuple[list[str], int]:
        """
        Возвращает (список имён артистов, количество).
        Делает минимальные запросы к /artists/{id} БЕЗ include/views.
        """
        count = len(artists_data)
        if count == 0:
            return [], 0

        if count == 1:
            # Для одного артиста делаем запрос, чтобы получить имя
            artist_id = artists_data[0].get("id")
            if not artist_id:
                return [], count
            try:
                artist = await self.get_artist(artist_id, include="", views="", limit=1)
                name = artist["data"][0]["attributes"]["name"]
                return [name], count
            except Exception as e:
                logger.warning(f"Failed to fetch artist {artist_id}: {e}")
                return [], count

        # Несколько артистов — параллельные запросы БЕЗ include
        tasks = [
            self.get_artist(artist["id"], include="", views="", limit=1)
            for artist in artists_data
            if "id" in artist
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        names: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Failed to fetch artist: {result}")
                continue
            try:
                name = result["data"][0]["attributes"]["name"]
                if name:
                    names.append(name)
            except (KeyError, IndexError, TypeError) as e:
                logger.warning(f"Malformed artist response: {e}")

        return names, count

    async def get_full_album(self, album_id: str) -> dict:
        """
        Возвращает альбом с полностью обогащёнными треками.
        Каждый трек в relationships.tracks.data будет иметь:
        - artistNames: list[str]
        - albumArtistNames: list[str]
        - artistsCount / albumArtistsCount
        - relationships.albums.data[0].attributes (recordLabel, upc, artistName)
        """
        log = logger.bind(action="get_full_album", album_id=album_id)

        album_response = await self.get_album(album_id)
        album_data_list = album_response.get("data")
        if not album_data_list:
            raise GamdlApiResponseError(
                f"No 'data' in album response for {album_id}",
                content=album_response,
                status_code=None,
            )

        album = album_data_list[0]

        # === Обогащаем артистов альбома ===
        album_artists_data = (
            album.get("relationships", {})
            .get("artists", {})
            .get("data", [])
        )
        album_artist_names, album_artists_count = await self._resolve_artist_names(
            album_artists_data
        )
        album["albumArtistNames"] = album_artist_names
        album["albumArtistsCount"] = album_artists_count
        if album_artist_names:
            album["attributes"]["artistName"] = ", ".join(album_artist_names)

        # === Обогащаем каждый трек ===
        tracks_data = (
            album.get("relationships", {})
            .get("tracks", {})
            .get("data", [])
        )

        if tracks_data:
            # Параллельное обогащение всех треков
            tasks = [
                self._enrich_album_track(track, album)
                for track in tracks_data
            ]
            enriched_tracks = await asyncio.gather(*tasks, return_exceptions=True)

            for i, result in enumerate(enriched_tracks):
                if isinstance(result, Exception):
                    logger.warning(
                        f"Failed to enrich track {tracks_data[i].get('id')}: {result}"
                    )
                    # Оставляем трек как есть, без обогащения
                    continue
                tracks_data[i] = result

        log.debug(
            "success",
            album_id=album_id,
            tracks_count=len(tracks_data),
            album_artists=album_artist_names,
        )

        # print(json.dumps(album, indent=2, ensure_ascii=False))

        return {"data": [album]}

    async def _enrich_album_track(self, track: dict, album: dict) -> dict:
        """
        Обогащает отдельный трек из альбома данными об артистах и альбоме.
        """
        # === Трековые артисты ===
        artists_data = (
            track.get("relationships", {})
            .get("artists", {})
            .get("data", [])
        )

        if artists_data:
            # Есть relationships.artists → резолвим по ID
            artist_names, artists_count = await self._resolve_artist_names(artists_data)
        else:
            # Нет relationships.artists → запрашиваем полный трек
            track_id = track.get("id")
            artist_names = []
            artists_count = 0

            if track_id:
                try:
                    full_song_response = await self.get_song(track_id)
                    full_song_list = full_song_response.get("data", [])
                    if full_song_list:
                        full_artists_data = (
                            full_song_list[0]
                            .get("relationships", {})
                            .get("artists", {})
                            .get("data", [])
                        )
                        artist_names, artists_count = await self._resolve_artist_names(
                            full_artists_data
                        )
                except Exception as e:
                    logger.warning(f"Failed to fetch full song {track_id}: {e}")

            # Fallback если ничего не помогло
            if not artist_names:
                raw_artist = track.get("attributes", {}).get("artistName", "")
                artist_names = [raw_artist] if raw_artist else []
                artists_count = len(artist_names)

        track["artistNames"] = artist_names
        track["artistsCount"] = artists_count

        if artist_names:
            track["attributes"]["artistName"] = ", ".join(artist_names)

        # === Альбомные артисты (из родительского альбома, без доп. запроса) ===
        track["albumArtistNames"] = album.get("albumArtistNames", [])
        track["albumArtistsCount"] = album.get("albumArtistsCount", 0)

        # === Встраиваем album relationship в трек ===
        if "relationships" not in track:
            track["relationships"] = {}

        album_attrs = album.get("attributes", {})
        track["relationships"]["albums"] = {
            "data": [
                {
                    "id": album.get("id"),
                    "type": "albums",
                    "attributes": {
                        "artistName": album_attrs.get("artistName", ""),
                        "recordLabel": album_attrs.get("recordLabel", ""),
                        "upc": album_attrs.get("upc", ""),
                        "name": album_attrs.get("name", ""),
                    },
                }
            ]
        }

        return track

    async def enrich_song_artists(self, song: dict) -> dict:
        """
        Обогащает песню:
        - artistNames: list[str]      ← массив для атомов ©ART
        - albumArtistNames: list[str] ← массив для атомов aART
        - artistName / albumArtist: str (для обратной совместимости)
        """
        # === TRACK ARTISTS ===
        song = song.get("data", [{}])[0]
        artists_data = (
            song.get("relationships", {})
            .get("artists", {})
            .get("data", [])
        )
        artist_names, artists_count = await self._resolve_artist_names(artists_data)

        # Сохраняем МАССИВ (для mutagen)
        song["artistNames"] = artist_names
        song["artistsCount"] = artists_count

        # Строковая версия (для совместимости)
        if artist_names:
            song["attributes"]["artistName"] = ", ".join(artist_names)

        # === ALBUM ARTISTS ===
        album_rel = (
            song.get("relationships", {})
            .get("albums", {})
            .get("data", [])
        )

        if album_rel:
            album_id = album_rel[0].get("id")
            if album_id:
                try:
                    album_response = await self.get_album(album_id)
                    album_data = album_response.get("data", [{}])[0]
                    album_attrs = album_data.get("attributes", {})
                    album_artists_data = (
                        album_data.get("relationships", {})
                        .get("artists", {})
                        .get("data", [])
                    )

                    album_artist_names, album_artists_count = (
                        await self._resolve_artist_names(album_artists_data)
                    )

                    # Сохраняем МАССИВ в песню
                    song["albumArtistNames"] = album_artist_names
                    song["albumArtistsCount"] = album_artists_count

                    # Обновляем attributes альбома в relationships
                    if "attributes" not in album_rel[0]:
                        album_rel[0]["attributes"] = {}

                    # Копируем нужные поля из полного ответа альбома
                    for key in ("artistName", "recordLabel", "upc"):
                        if key in album_attrs:
                            album_rel[0]["attributes"][key] = album_attrs[key]

                    # Перезаписываем artistName массивом через join
                    if album_artist_names:
                        album_rel[0]["attributes"]["artistName"] = ", ".join(album_artist_names)
                        song["attributes"]["albumArtist"] = ", ".join(album_artist_names)
                    else:
                        song["attributes"]["albumArtist"] = album_attrs.get("artistName", "")

                except GamdlApiResponseError as e:
                    logger.warning(f"Failed to fetch album {album_id}: {e}")
                    song["albumArtistNames"] = []
                    song["albumArtistsCount"] = 0
        else:
            song["albumArtistNames"] = []
            song["albumArtistsCount"] = 0

        # songResult: json = {}
        # songResult["data"][0] = song
        songResult: dict = {"data": [song]}
        return songResult

    async def enrich_album_artists(self, album: dict) -> dict:
        """Аналогичное обогащение для альбома."""
        artists_data = (
            album.get("relationships", {})
            .get("artists", {})
            .get("data", [])
        )

        art_names, artists_count = await self._resolve_artist_names(artists_data)

        if art_names:
            album["attributes"]["artistName"] = ", ".join(art_names)

        album["artistsCount"] = artists_count
        return album

    async def get_full_song(self, song_id: str) -> dict:
        song_response = await self.get_song(song_id)
        
        # Обогащаем артистами + альбомом за минимальное время
        song = await self.enrich_song_artists(song_response)

        # print(json.dumps(song, indent=2, ensure_ascii=False))
        
        return song

    @alru_cache(maxsize=256)
    async def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict:
        log = logger.bind(action="get_song", song_id=song_id)

        song = await self._amp_request(
            APPLE_MUSIC_SONG_API_URI.format(
                storefront=self.storefront,
                song_id=song_id,
            ),
            {
                "extend": extend,
                "include": include,
            },
        )

        return song

    async def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict:
        log = logger.bind(action="get_music_video", music_video_id=music_video_id)

        music_video = await self._amp_request(
            APPLE_MUSIC_MUSIC_VIDEO_API_URI.format(
                storefront=self.storefront,
                music_video_id=music_video_id,
            ),
            {
                "include": include,
            },
        )

        log.debug("success", music_video=music_video)

        return music_video

    async def get_uploaded_video(
        self,
        uploaded_video_id: str,
    ) -> dict:
        log = logger.bind(
            action="get_uploaded_video", uploaded_video_id=uploaded_video_id
        )

        uploaded_video = await self._amp_request(
            APPLE_MUSIC_UPLOADED_VIDEO_API_URL.format(
                storefront=self.storefront,
                uploaded_video_id=uploaded_video_id,
            )
        )

        log.debug("success", uploaded_video=uploaded_video)

        return uploaded_video

    @alru_cache(maxsize=256)
    async def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_album", album_id=album_id)

        album = await self._amp_request(
            APPLE_MUSIC_ALBUM_API_URI.format(
                storefront=self.storefront,
                album_id=album_id,
            ),
            {
                "extend": extend,
            },
        )

        # print(json.dumps(album, indent=2, ensure_ascii=False))

        log.debug("success", album=album)

        return album

    async def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_playlist", playlist_id=playlist_id)

        playlist = await self._amp_request(
            APPLE_MUSIC_PLAYLIST_API_URI.format(
                storefront=self.storefront,
                playlist_id=playlist_id,
            ),
            {
                "limit[tracks]": limit_tracks,
                "extend": extend,
            },
        )

        log.debug("success", playlist=playlist)

        return playlist

    @alru_cache(maxsize=256)
    async def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        limit: int = 100,
    ) -> dict:
        log = logger.bind(action="get_artist", artist_id=artist_id)

        artist = await self._amp_request(
            APPLE_MUSIC_ARTIST_API_URI.format(
                storefront=self.storefront,
                artist_id=artist_id,
            ),
            {
                "include": include,
                "views": views,
                **{
                    f"limit[{_include}]": limit
                    for _include in [*include.split(","), *views.split(",")]
                },
            },
        )

        log.debug("success", artist=artist)

        return artist

    async def get_library_song(
        self,
        song_id: str,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_song", song_id=song_id)

        song = await self._amp_request(
            APPLE_MUSIC_LIBRARY_SONG_API_URI.format(
                song_id=song_id,
            ),
            {
                "include": include,
                "extend": extend,
            },
        )

        log.debug("success", song=song)

        return song

    async def get_library_music_video(
        self,
        music_video_id: str,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(
            action="get_library_music_video", music_video_id=music_video_id
        )

        music_video = await self._amp_request(
            APPLE_MUSIC_LIBRARY_MUSIC_VIDEO_API_URI.format(
                music_video_id=music_video_id,
            ),
            {
                "include": include,
            },
        )

        log.debug("success", music_video=music_video)

        return music_video

    async def get_library_album(
        self,
        album_id: str,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_album", album_id=album_id)

        album = await self._amp_request(
            APPLE_MUSIC_LIBRARY_ALBUM_API_URI.format(
                album_id=album_id,
            ),
            {
                "include": include,
                "extend": extend,
            },
        )

        log.debug("success", album=album)

        return album

    async def get_library_playlist(
        self,
        playlist_id: str,
        include: str = "catalog,tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_playlist", playlist_id=playlist_id)

        playlist = await self._amp_request(
            APPLE_MUSIC_LIBRARY_PLAYLIST_API_URI.format(
                playlist_id=playlist_id,
            ),
            {
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
                "extend": extend,
            },
        )

        log.debug("success", playlist=playlist)

        return playlist

    async def get_library_songs(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_songs")

        library_songs = await self._amp_request(
            APPLE_MUSIC_LIBRARY_SONGS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
                "extend": extend,
            },
        )

        log.debug("success", library_songs=library_songs)

        return library_songs

    async def get_library_music_videos(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(action="get_library_music_videos")

        library_music_videos = await self._amp_request(
            APPLE_MUSIC_LIBRARY_MUSIC_VIDEOS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
            },
        )

        log.debug("success", library_music_videos=library_music_videos)

        return library_music_videos

    async def get_library_albums(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(action="get_library_albums")

        library_albums = await self._amp_request(
            APPLE_MUSIC_LIBRARY_ALBUMS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
            },
        )

        log.debug("success", library_albums=library_albums)

        return library_albums

    async def get_library_playlists(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(action="get_library_playlists")

        library_playlists = await self._amp_request(
            APPLE_MUSIC_LIBRARY_PLAYLISTS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
            },
        )

        log.debug("success", library_playlists=library_playlists)

        return library_playlists

    async def get_search_results(
        self,
        term: str,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        log = logger.bind(action="get_search_results", term=term, types=types)

        search_results = await self._amp_request(
            APPLE_MUSIC_SEARCH_API_URI.format(
                storefront=self.storefront,
            ),
            {
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )

        log.debug("success", search_results=search_results)

        return search_results

    async def get_assets(
        self,
        media_id: str,
        kind: str = "song",
        include_license_urls: bool = True,
        hls_encryption: str = "CBC",
        hls_profile: str = "enhancedHls",
    ) -> dict:
        log = logger.bind(
            action="get_assets",
            media_id=media_id,
            kind=kind,
            include_license_urls=include_license_urls,
            hls_encryption=hls_encryption,
            hls_profile=hls_profile,
        )

        params = {
            "id": media_id,
            "kind": kind,
            "includeLicenseUrls": include_license_urls,
            "hlsEncryption": hls_encryption,
        }
        if hls_profile:
            params["hlsProfile"] = hls_profile

        assets = await self._amp_request(
            APPLE_MUSIC_ASSETS_API_URI,
            params,
        )

        log.debug("success", assets=assets)

        return assets

    async def get_extended_api_data(
        self,
        next_uri: str | None,
        href_uri: str,
    ) -> dict:
        log = logger.bind(
            action="extend_api_data", next_uri=next_uri, href_uri=href_uri
        )

        if not next_uri:
            log.debug("no_next_uri")
            return

        href_params = parse_qs(urlparse(href_uri).query)
        next_params = parse_qs(urlparse(next_uri).query)

        if href_params.get("limit"):
            limit = int(href_params["limit"][0])
        else:
            limit = None

        extended_data = await self._amp_request(
            urlparse(next_uri).path,
            {
                **({"limit": limit} if limit else {}),
                **{k: v for k, v in next_params.items() if k not in ["limit"]},
            },
        )

        log.debug("success", extended_data=extended_data)

        return extended_data

    async def get_webplayback(
        self,
        track_id: str,
        is_library: bool = False,
    ) -> dict:
        log = logger.bind(action="get_webplayback", track_id=track_id)

        response = None

        if is_library:
            request_body = {
                "universalLibraryId": track_id,
            }
        else:
            request_body = {
                "salableAdamId": track_id,
            }
        request_body["language"] = self.language

        try:
            response = await self.client.post(
                APPLE_MUSIC_WEBPLAYBACK_API_URL,
                json=request_body,
            )
            response.raise_for_status()
            webplayback = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching webplayback data",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if "dialog" in webplayback:
            raise GamdlApiResponseError(
                "Error fetching webplayback data",
                content=webplayback["dialog"],
            )

        log.debug("success", webplayback=webplayback)

        return webplayback

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        key_system: str = "com.widevine.alpha",
        is_library: bool = False,
    ) -> dict:
        log = logger.bind(action="get_license_exchange", track_id=track_id)

        response = None
        try:
            response = await self.client.post(
                APPLE_MUSIC_LICENSE_API_URL,
                json={
                    "challenge": challenge,
                    "key-system": key_system,
                    "uri": track_uri,
                    "adamId": track_id,
                    "isLibrary": is_library,
                    "user-initiated": True,
                },
            )
            response.raise_for_status()
            license_exchange = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching license exchange data",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if license_exchange.get("status") != 0:
            raise GamdlApiResponseError(
                "Error fetching license exchange data",
                content=response.text,
                status_code=response.status_code,
            )

        log.debug("success", license_exchange=license_exchange)

        return license_exchange
