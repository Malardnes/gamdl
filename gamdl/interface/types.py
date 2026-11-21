import datetime
from dataclasses import dataclass
from typing import Any

from .enums import MediaFileFormat, MediaRating, MediaType


@dataclass
class Lyrics:
    synced: str = None
    unsynced: str = None
    lyricist: str = None


import re
from dataclasses import dataclass, field


@dataclass
class ParsedArtists:
    """Результат парсинга артистов из заголовка трека."""
    main: list[str] = field(default_factory=list)       # Основные артисты
    featured: list[str] = field(default_factory=list)     # Featured артисты
    remixers: list[str] = field(default_factory=list)     # Ремиксеры / VIP / Edit
    extras: list[str] = field(default_factory=list)       # Прочее (Mixed, From...)


# === РЕГУЛЯРКИ ===
# Порядок важен: более специфичные паттерны ПЕРЕД более общими

_FEATURED_RE = re.compile(
    r'\s*[\(\[]'                        # открывающая скобка ( или [
    r'(?:feat\.?|ft\.?|featuring|with)\s+'  # ключевое слово
    r'(.+?)'                            # содержимое (ленивое)
    r'[\)\]]',                          # закрывающая скобка
    re.IGNORECASE,
)

_REMIX_RE = re.compile(
    r'\s*[\(\[]'                        # скобка
    r'(.+?)'                            # имя ремиксера
    r'\s+(?:remix|vip|edit|bootleg|rework|flip|mix)'  # тип
    r'[\)\]]',                          # закрывающая скобка
    re.IGNORECASE,
)

_MIXED_RE = re.compile(
    r'\s*[\(\[]mixed[\)\]]',
    re.IGNORECASE,
)

_FROM_RE = re.compile(
    r'\s*[\(\[]'
    r'(?:from|from\s+the)\s+'
    r'(.+?)'
    r'[\)\]]',
    re.IGNORECASE,
)

_BUSTED_RE = re.compile(
    r'\s*[\(\[]'
    r'(?:busted|busted\s+by|remixed)\s+(?:by\s+)?'
    r'(.+?)'
    r'[\)\]]',
    re.IGNORECASE,
)


@dataclass
class MediaTags:
    album: str = None
    album_artist: str | list[str] = None
    album_id: int = None
    album_sort: str = None
    artist: str | list[str] = None
    artist_id: int = None
    artist_sort: str = None
    comment: str = None
    compilation: bool = None
    composer: str = None
    composer_id: int = None
    composer_sort: str = None
    copyright: str = None
    date: datetime.date | str = None
    disc: int = None
    disc_total: int = None
    gapless: bool = None
    genre: str = None
    genre_id: int = None
    lyrics: str = None
    media_type: MediaType = None
    rating: MediaRating = None
    storefront: str = None
    title: str = None
    title_id: int = None
    title_sort: str = None
    track: int = None
    track_total: int = None
    xid: str = None
    director: str = None
    www: str = None
    isrc: str = None
    label: str = None
    lyricist: str = None
    encoding_tool: str = None
    encoder: str = None
    upc: str = None
    featured_artists: str | list[str] = None
    remixed_artists: str | list[str] = None
    initial_key: str = None
    bpm: int = None
    mood: str = None
    workName: str = None
    movementName: str = None
    movementCount: str = None
    movementNumber: str = None

    @staticmethod
    def _ensure_list(value: str | list[str] | None) -> list[str] | None:
        """
        Приводит значение к list[str] для mutagen MP4.
        - None → None
        - "A, B" → ["A, B"]  (строка остаётся одним элементом)
        - ["A", "B"] → ["A", "B"]
        """
        if value is None:
            return None
        if isinstance(value, list):
            return value if value else None
        return [value]

    def as_mp4_tags(self, date_format: str | None = None) -> dict:
        # === DISC ===
        disc_val = self.disc if self.disc is not None else 0
        disc_total_val = self.disc_total if self.disc_total is not None else 0
        disc_mp4 = [(disc_val, disc_total_val)] if (disc_val or disc_total_val) else None

        # === TRACK ===
        track_val = self.track if self.track is not None else 0
        track_total_val = self.track_total if self.track_total is not None else 0
        track_mp4 = [(track_val, track_total_val)] if (track_val or track_total_val) else None

        if isinstance(self.date, datetime.date):
            if date_format is None:
                date_mp4 = self.date.isoformat()
            else:
                date_mp4 = self.date.strftime(date_format)
        elif isinstance(self.date, str):
            date_mp4 = self.date
        else:
            date_mp4 = None

        artist_list = self._ensure_list(self.artist)
        album_artist_list = self._ensure_list(self.album_artist)

        mp4_tags = {
            "\xa9alb": self.album,
            "aART": album_artist_list,
            "plID": self.album_id,
            "soal": self.album_sort,
            "\xa9ART": artist_list,
            "atID": self.artist_id,
            "soar": self.artist_sort,
            "\xa9cmt": self.comment,
            "cpil": bool(self.compilation) if self.compilation is not None else None,
            "\xa9wrt": self.composer,
            "cmID": self.composer_id,
            "soco": self.composer_sort,
            "cprt": self.copyright,
            "\xa9day": date_mp4,
            "disk": disc_mp4,
            "pgap": bool(self.gapless) if self.gapless is not None else None,
            "\xa9gen": self.genre,
            "\xa9lyr": self.lyrics,
            "geID": self.genre_id,
            "stik": int(self.media_type) if self.media_type is not None else None,
            "rtng": int(self.rating) if self.rating is not None else None,
            "sfID": self.storefront,
            "\xa9nam": self.title,
            "cnID": self.title_id,
            "sonm": self.title_sort,
            "trkn": track_mp4,
            "xid ": self.xid,
            "\xa9con": self.director,
            "\xa9dir": self.director,
            "----:com.apple.iTunes:WWW": self.www,
            "----:com.apple.iTunes:ISRC": self.isrc,
            "----:com.apple.iTunes:PUBLISHER": self.label,
            "----:com.apple.iTunes:LABEL": self.label,
            "----:com.apple.iTunes:LYRICIST": self.lyricist,
            "----:com.apple.iTunes:ENCODER": self.encoding_tool,
            "----:com.apple.iTunes:ENCODEDBY": self.encoder,
            "----:com.apple.iTunes:UPC": self.upc,
            "----:com.apple.iTunes:FEATURING": self.featured_artists,
            "----:com.apple.iTunes:REMIXER": self.remixed_artists,
            "----:com.apple.iTunes:INITIALKEY": self.initial_key,
            "tmpo": self.bpm,
            "mood": self.mood,
            "\xa9wrk": self.workName,
            "\xa9mvn": self.movementName,
            "\xa9mvc": self.movementCount,
            "\xa9mvi": self.movementNumber,
        }

        return_dict = {}
        for k, v in mp4_tags.items():
            if v is None:
                continue
            
            if isinstance(v, bool):
                return_dict[k] = v
            elif k.startswith("----:"):
                if isinstance(v, list):
                    # Если вдруг пришел список артистов, объединяем в строку через запятую
                    return_dict[k] = "; ".join(map(str, v)).encode("utf-8")
                else:
                    return_dict[k] = [v.encode("utf-8") if isinstance(v, str) else v]
            elif isinstance(v, list):
                return_dict[k] = v
            else:
                return_dict[k] = [v]
                
        return return_dict


@dataclass
class PlaylistTags:
    artist: str = None
    playlist_id: int = None
    title: str = None
    track: int = None


@dataclass
class StreamInfo:
    stream_url: str = None
    widevine_pssh: str = None
    playready_pssh: str = None
    fairplay_key: str = None
    codec: str = None
    width: int = None
    height: int = None
    drm_free: bool = False
    use_cenc: bool = False
    use_single_content_key: bool = True


@dataclass
class StreamInfoAv:
    media_id: str = None
    video_track: StreamInfo = None
    audio_track: StreamInfo = None
    file_format: MediaFileFormat = None


@dataclass
class DecryptionKey:
    kid: str = None
    key: str = None


@dataclass
class DecryptionKeyAv:
    video_track: DecryptionKey = None
    audio_track: DecryptionKey = None


@dataclass
class Cover:
    template_url: str = None
    file_extension: str = None
    url: str = None


@dataclass
class AppleMusicMedia:
    media_id: str
    is_library: bool = False
    index: int = 0
    total: int = 0
    partial: bool = True
    media_metadata: dict | None = None
    error: BaseException | None = None
    playlist_metadata: dict | None = None
    playlist_tags: PlaylistTags | None = None
    extra_tags: dict | None = None
    cover: Cover | None = None
    lyrics: Lyrics | None = None
    tags: MediaTags | None = None
    stream_info: StreamInfoAv | None = None
    decryption_key: DecryptionKeyAv | None = None


@dataclass
class AppleMusicUrlInfo:
    storefront: str = None
    type: str = None
    slug: str = None
    id: str = None
    sub_id: str = None
    library_storefront: str = None
    library_type: str = None
    library_id: str = None
