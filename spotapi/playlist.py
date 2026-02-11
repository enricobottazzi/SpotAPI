from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from spotapi.login import Login
from spotapi.user import User
from spotapi.client import BaseClient
from collections.abc import Mapping, Generator
from spotapi.types.annotations import enforce
from spotapi.exceptions import PlaylistError
from spotapi.http.request import TLSClient

logger = logging.getLogger(__name__)

# Ensure timing logs are visible even when spotapi is used as a dependency.
# If the consumer has already configured handlers on this logger (or a parent),
# this block is skipped so we don't duplicate output.
if not logger.handlers and not logging.getLogger("spotapi").handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

__all__ = ["PublicPlaylist", "PrivatePlaylist", "PlaylistError"]


@enforce
class PublicPlaylist:
    """
    Allows you to get all public information on a playlist.
    No login is required.

    Parameters
    ----------
    playlist (Optional[str]): The Spotify URI of the playlist.
    client (TLSClient): An instance of TLSClient to use for requests.
    """

    __slots__ = (
        "base",
        "playlist_id",
        "playlist_link",
    )

    def __init__(
        self,
        playlist: str,
        /,
        *,
        client: TLSClient = TLSClient("chrome_120", "", auto_retries=3),
        language: str = "en",
    ) -> None:
        self.base = BaseClient(client=client, language=language)
        self.playlist_id = (
            playlist.split("playlist/")[-1] if "playlist" in playlist else playlist
        )
        self.playlist_link = f"https://open.spotify.com/playlist/{self.playlist_id}"

    def get_playlist_info(
        self,
        limit: int = 25,
        *,
        offset: int = 0,
        enable_watch_feed_entrypoint: bool = False,
    ) -> Mapping[str, Any]:
        """Gets the public playlist information"""
        url = "https://api-partner.spotify.com/pathfinder/v1/query"

        # Hardcoded hash avoids expensive JS bundle scraping via part_hash().
        # If Spotify rotates this hash, update it from browser DevTools or
        # fall back to: self.base.part_hash("fetchPlaylist")
        sha256_hash = "7982b11e21535cd2594badc40030b745671b61a1fa66766e569d45e6364f3422"

        params = {
            "operationName": "fetchPlaylist",
            "variables": json.dumps(
                {
                    "uri": f"spotify:playlist:{self.playlist_id}",
                    "offset": offset,
                    "limit": limit,
                    "enableWatchFeedEntrypoint": enable_watch_feed_entrypoint,
                }
            ),
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": sha256_hash,
                    }
                }
            ),
        }

        logger.info("get_playlist_info: starting for playlist_id=%s", self.playlist_id)
        t0 = time.perf_counter()
        resp = self.base.client.post(url, params=params, authenticate=True)
        logger.info("get_playlist_info: response received in %.3fs", time.perf_counter() - t0)

        if resp.fail:
            raise PlaylistError("Could not get playlist info", error=resp.error.string)

        if not isinstance(resp.response, Mapping):
            raise PlaylistError("Invalid JSON")

        return resp.response

    def paginate_playlist(self) -> Generator[Mapping[str, Any], None, None]:
        """
        Generator that fetches playlist information in chunks

        NOTE: If total_tracks <= 343, then there is no need to paginate.
        """
        UPPER_LIMIT: int = 343
        logger.info("paginate_playlist: starting for playlist_id=%s", self.playlist_id)
        overall_start = time.perf_counter()

        # First fetch
        t0 = time.perf_counter()
        playlist = self.get_playlist_info(limit=UPPER_LIMIT)
        elapsed_fetch = time.perf_counter() - t0
        total_count: int = playlist["data"]["playlistV2"]["content"]["totalCount"]
        logger.info(
            "paginate_playlist: initial fetch completed in %.3fs, total_count=%d",
            elapsed_fetch,
            total_count,
        )

        yield playlist["data"]["playlistV2"]["content"]

        if total_count <= UPPER_LIMIT:
            overall_elapsed = time.perf_counter() - overall_start
            logger.info(
                "paginate_playlist: completed in %.3fs (single page, no pagination needed)",
                overall_elapsed,
            )
            return

        offset = UPPER_LIMIT
        page_num = 1
        while offset < total_count:
            page_num += 1
            t0 = time.perf_counter()
            result = self.get_playlist_info(limit=UPPER_LIMIT, offset=offset)
            elapsed_fetch = time.perf_counter() - t0
            logger.info(
                "paginate_playlist: page %d fetch (offset=%d) completed in %.3fs",
                page_num,
                offset,
                elapsed_fetch,
            )
            yield result["data"]["playlistV2"]["content"]
            offset += UPPER_LIMIT

        overall_elapsed = time.perf_counter() - overall_start
        logger.info(
            "paginate_playlist: completed in %.3fs (%d pages total)",
            overall_elapsed,
            page_num,
        )

    @staticmethod
    def _query_user_playlists(
        base: BaseClient,
        username: str,
        limit: int,
        offset: int,
        sha256_hash: str,
    ) -> Mapping[str, Any]:
        """Internal helper that performs a single user playlists query using an existing BaseClient."""
        url = "https://api-partner.spotify.com/pathfinder/v1/query"
        params = {
            "operationName": "queryUserPagination",
            "variables": json.dumps(
                {
                    "uri": f"spotify:user:{username}",
                    "publicPlaylistsV2Limit": limit,
                    "publicPlaylistsV2Offset": offset,
                }
            ),
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": sha256_hash,
                    }
                }
            ),
        }

        resp = base.client.get(url, params=params, authenticate=True)

        if resp.fail:
            raise PlaylistError(
                "Could not get user public playlists", error=resp.error.string
            )

        if not isinstance(resp.response, Mapping):
            raise PlaylistError("Invalid JSON")

        return resp.response

    @staticmethod
    def paginate_user_playlists(
        username: str,
        /,
        *,
        client: TLSClient = TLSClient("chrome_120", "", auto_retries=3),
        language: str = "en",
    ) -> Generator[Mapping[str, Any], None, None]:
        """
        Generator that fetches all public playlists for a user in chunks.

        Parameters
        ----------
        username : str
            The Spotify username to look up.
        client : TLSClient
            An instance of TLSClient to use for requests.
        language : str
            ISO 639-1 language code for the response.

        Yields
        ------
        Mapping[str, Any]
            Each page of the user's public playlists.
        """
        UPPER_LIMIT: int = 50

        # Create the BaseClient ONCE, reuse for all pages
        base = BaseClient(client=client, language=language)

        # Hardcoded hash avoids expensive JS bundle scraping via part_hash().
        # If Spotify rotates this hash, update it from browser DevTools or
        # fall back to: base.part_hash("queryUserPagination")
        sha256_hash = "5b1399f199da0b45e368dac387ed4688e84a1c59111c835d0776e3a59d8a4395"

        resp = PublicPlaylist._query_user_playlists(base, username, UPPER_LIMIT, 0, sha256_hash)

        playlists_data = resp["data"]["user"]["publicPlaylistsV2"]
        total_count: int = playlists_data["totalCount"]

        yield playlists_data

        if total_count <= UPPER_LIMIT:
            return

        offset = UPPER_LIMIT
        while offset < total_count:
            page = PublicPlaylist._query_user_playlists(base, username, UPPER_LIMIT, offset, sha256_hash)
            yield page["data"]["user"]["publicPlaylistsV2"]
            offset += UPPER_LIMIT


class PrivatePlaylist:
    """
    Methods on playlists that you can only do whilst logged in.

    Parameters
    ----------
    login (Login): The login object to use
    playlist (Optional[str]): The Spotify URI of the playlist.
    """

    __slots__ = (
        "base",
        "login",
        "user",
        "_playlist",
        "playlist_id",
    )

    def __init__(
        self,
        login: Login,
        playlist: str | None = None,
        *,
        language: str = "en",
    ) -> None:
        if not login.logged_in:
            raise ValueError("Must be logged in")

        if playlist:
            self.playlist_id = (
                playlist.split("playlist/")[-1] if "playlist" in playlist else playlist
            )

        self.base = BaseClient(login.client, language=language)
        self.login = login
        self.user = User(login)
        # We need to check if a user can use a method
        self._playlist: bool = bool(playlist)

    def set_playlist(self, playlist: str) -> None:
        if "playlist:" in playlist:
            playlist = playlist.split("playlist:")[-1]

        if not playlist:
            raise ValueError("Playlist not set")

        setattr(self, "playlist_id", playlist)
        self._playlist = True

    def add_to_library(self) -> None:
        """Adds the playlist to your library"""
        if not self._playlist:
            raise ValueError("Playlist not set")

        url = f"https://spclient.wg.spotify.com/playlist/v2/user/{self.user.username}/rootlist/changes"
        payload = {
            "deltas": [
                {
                    "ops": [
                        {
                            "kind": 2,
                            "add": {
                                "items": [
                                    {
                                        "uri": f"spotify:playlist:{self.playlist_id}",
                                        "attributes": {
                                            "timestamp": int(time.time()),
                                            "formatAttributes": [],
                                            "availableSignals": [],
                                        },
                                    }
                                ],
                                "addFirst": True,
                            },
                        }
                    ],
                    "info": {"source": {"client": 5}},
                }
            ],
            "wantResultingRevisions": False,
            "wantSyncResult": False,
            "nonces": [],
        }

        resp = self.login.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlaylistError(
                "Could not add playlist to library", error=resp.error.string
            )

    def remove_from_library(self) -> None:
        """Removes the playlist from your library"""
        if not self._playlist:
            raise ValueError("Playlist not set")

        url = f"https://spclient.wg.spotify.com/playlist/v2/user/{self.user.username}/rootlist/changes"
        payload = {
            "deltas": [
                {
                    "ops": [
                        {
                            "kind": 3,
                            "rem": {
                                "items": [
                                    {"uri": f"spotify:playlist:{self.playlist_id}"}
                                ],
                                "itemsAsKey": True,
                            },
                        }
                    ],
                    "info": {"source": {"client": 5}},
                }
            ],
            "wantResultingRevisions": False,
            "wantSyncResult": False,
            "nonces": [],
        }

        resp = self.login.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlaylistError(
                "Could not remove playlist from library", error=resp.error.string
            )

    def delete_playlist(self) -> None:
        """Deletes the playlist from your library"""
        # They are the same requests
        return self.remove_from_library()

    def get_library(self, limit: int = 50, /) -> Mapping[str, Any]:
        """Gets all the playlists in your library"""
        url = "https://api-partner.spotify.com/pathfinder/v1/query"
        params = {
            "operationName": "libraryV3",
            "variables": json.dumps(
                {
                    "filters": [],
                    "order": None,
                    "textFilter": "",
                    "features": ["LIKED_SONGS", "YOUR_EPISODES", "PRERELEASES"],
                    "limit": limit,
                    "offset": 0,
                    "flatten": False,
                    "expandedFolders": [],
                    "folderUri": None,
                    "includeFoldersWhenFlattening": True,
                }
            ),
            "extensions": json.dumps(
                {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": self.base.part_hash("libraryV3"),
                    }
                }
            ),
        }

        resp = self.login.client.post(url, params=params, authenticate=True)

        if resp.fail:
            raise PlaylistError("Could not get library", error=resp.error.string)

        return resp.response

    def _stage_create_playlist(self, name: str) -> str:
        url = "https://spclient.wg.spotify.com/playlist/v2/playlist"
        payload = {
            "ops": [
                {
                    "kind": 6,
                    "updateListAttributes": {
                        "newAttributes": {
                            "values": {
                                "name": name,
                                "formatAttributes": [],
                                "pictureSize": [],
                            },
                            "noValue": [],
                        }
                    },
                }
            ]
        }

        resp = self.login.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlaylistError(
                "Could not stage create playlist", error=resp.error.string
            )

        pattern = r"spotify:playlist:[a-zA-Z0-9]+"
        matched = re.search(pattern, resp.response)

        if not matched:
            raise PlaylistError("Could not find desired playlist ID")

        return matched.group(0)

    def create_playlist(self, name: str) -> str:
        """Creates a new playlist"""
        playlist_id = self._stage_create_playlist(name)
        url = f"https://spclient.wg.spotify.com/playlist/v2/user/{self.user.username}/rootlist/changes"
        payload = {
            "deltas": [
                {
                    "ops": [
                        {
                            "kind": 2,
                            "add": {
                                "items": [
                                    {
                                        "uri": playlist_id,
                                        "attributes": {
                                            "timestamp": int(time.time()),
                                            "formatAttributes": [],
                                            "availableSignals": [],
                                        },
                                    }
                                ],
                                "addFirst": True,
                            },
                        }
                    ],
                    "info": {"source": {"client": 5}},
                }
            ],
            "wantResultingRevisions": False,
            "wantSyncResult": False,
            "nonces": [],
        }

        resp = self.login.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlaylistError("Could not create playlist", error=resp.error.string)

        return playlist_id

    def recommended_songs(self, num_songs: int = 20) -> Mapping[str, Any]:
        """Gets the recommended songs for the playlist"""
        url = "https://spclient.wg.spotify.com/playlistextender/extendp/"
        payload = {
            "playlistURI": f"spotify:playlist:{self.playlist_id}",
            "trackSkipIDs": [],
            "numResults": num_songs,
        }
        resp = self.login.client.post(url, json=payload, authenticate=True)

        if resp.fail:
            raise PlaylistError(
                "Could not get recommended songs", error=resp.error.string
            )

        return resp.response
