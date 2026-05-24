#!/usr/bin/env python3
"""
RomM to RetroPie Metadata Sync

Pulls metadata from a RomM server and generates EmulationStation gamelist.xml files
for use with RetroPie.

Usage:
    python3 romm_sync.py --help
    python3 romm_sync.py --server https://roms.proveaux.net --user admin --password secret
"""

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
import re
from typing import Optional
import logging
import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Force unbuffered output for real-time progress display (especially when piped to zenity)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)


def setup_logging():
    """
    Configure logging to write to both file and console.
    
    Log files are stored in the ~/romm-sync directory with timestamps.
    """
    log_dir = Path.home() / "romm-sync"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"romm-sync_{timestamp}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    logging.info(f"Log file: {log_file}")
    return log_file


# Target system configurations
TARGET_CONFIGS = {
    "retropie": {
        "name": "RetroPie",
        "gamelist_path": "./.emulationstation/gamelists",
        "images_path": "~/.emulationstation/downloaded_images",
        "roms_path": "~/RetroPie/roms",
        "image_subdir": "",  # Images go directly in platform folder
    },
    "steamdeck": {
        "name": "SteamDeck (ES-DE)",
        "gamelist_path": "~/ES-DE/gamelists",  # Standard ES-DE location (not configurable)
        "images_path": None,  # Will be detected from ES-DE settings
        "roms_path": None,  # Will be detected from ES-DE settings
        "image_subdir": "covers",  # ES-DE uses covers/ subdirectory
    },
}

# Default gamelist output path (for backward compatibility)
GAMELIST_OUTPUT_PATH = "./.emulationstation/gamelists"

# Platform mapping: RomM platform slug -> EmulationStation folder name
# Compatible with both RetroPie and ES-DE (SteamDeck)
PLATFORM_MAP = {
    "3do": "3do",
    "amiga": "amiga",
    "amstrad-cpc": "amstradcpc",
    "arcade": "arcade",
    "atari-2600": "atari2600",
    "atari-5200": "atari5200",
    "atari-7800": "atari7800",
    "atari-jaguar": "atarijaguar",
    "atari-lynx": "atarilynx",
    "atari-st": "atarist",
    "colecovision": "coleco",
    "commodore-64": "c64",
    "dreamcast": "dreamcast",
    "famicom-disk-system": "fds",
    "game-boy": "gb",
    "game-boy-advance": "gba",
    "game-boy-color": "gbc",
    "game-gear": "gamegear",
    "gamecube": "gc",
    "genesis": "megadrive",
    "intellivision": "intellivision",
    "master-system": "mastersystem",
    "mega-drive": "megadrive",
    "msx": "msx",
    "n64": "n64",
    "neo-geo": "neogeo",
    "neo-geo-cd": "neogeocd",
    "neo-geo-pocket": "ngp",
    "neo-geo-pocket-color": "ngpc",
    "nes": "nes",
    "nintendo-3ds": "n3ds",
    "new-nintendo-3ds": "n3ds",
    "nintendo-ds": "nds",
    "nintendo-switch": "switch",
    "pc-engine": "pcengine",
    "pc-engine-cd": "pcenginecd",
    "psx": "psx",
    "ps2": "ps2",
    "ps3": "ps3",
    "psp": "psp",
    "psvita": "psvita",
    "saturn": "saturn",
    "sega-32x": "sega32x",
    "sega-cd": "segacd",
    "snes": "snes",
    "super-famicom": "snes",
    "turbografx-16": "tg16",
    "turbografx-cd": "tg-cd",
    "vectrex": "vectrex",
    "virtual-boy": "virtualboy",
    "wii": "wii",
    "wii-u": "wiiu",
    "wonderswan": "wonderswan",
    "wonderswan-color": "wonderswancolor",
    "xbox": "xbox",
    "xbox-360": "xbox360",
    "zx-spectrum": "zxspectrum",
    # ES-DE specific additions
    "playstation": "psx",
    "playstation-2": "ps2",
    "playstation-3": "ps3",
    "playstation-portable": "psp",
    "playstation-vita": "psvita",
}


class RomMClient:
    """Client for interacting with RomM API."""

    def __init__(self, server_url: str, username: str, password: str):
        self.server_url = server_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.session = requests.Session()
        self.session.auth = self.auth
        
        # Caches to prevent redundant API calls
        self._collections_cache = None
        self._collection_roms_cache = {}
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _get(self, endpoint: str, params: dict = None, timeout: int = 120) -> dict:
        """Make a GET request to the API."""
        url = f"{self.server_url}/api{endpoint}"
        response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def get_platforms(self) -> list:
        """Get all platforms from RomM."""
        return self._get("/platforms")
    
    def get_collections(self) -> list:
        """Get all collections from RomM."""
        if self._collections_cache is None:
            self._collections_cache = self._get("/collections")
        return self._collections_cache
    
    def get_favorites_collection_id(self) -> Optional[int]:
        """Get the ID of the Favourites/Favorites collection."""
        collections = self.get_collections()
        for collection in collections:
            name = collection.get('name', '').strip().lower()
            if name in ('favourites', 'favorites'):
                return collection.get('id')
        return None
    
    def get_kid_friendly_collection_id(self) -> Optional[int]:
        """Get the ID of the Kid Friendly collection."""
        collections = self.get_collections()
        for collection in collections:
            if 'kid' in collection.get('name', '').lower():
                return collection.get('id')
        return None
    
    def get_kid_friendly_rom_ids(self) -> set:
        """Get set of ROM IDs that are in the Kid Friendly collection.
        
        Returns:
            Set of ROM IDs marked as kid friendly.
        """
        collection_id = self.get_kid_friendly_collection_id()
        if not collection_id:
            return set()
        
        # Get all ROMs in the kid friendly collection
        roms_response = self.get_roms(collection_id=collection_id, limit=10000)
        if isinstance(roms_response, dict) and "items" in roms_response:
            roms = roms_response["items"]
        elif isinstance(roms_response, list):
            roms = roms_response
        else:
            return set()
        
        return {rom.get('id') for rom in roms if rom.get('id')}

    def get_roms(self, platform_id: int = None, limit: int = 1000, favorites_only: bool = False, collection_id: int = None) -> list:
        """Get all ROMs for a platform.
        
        Args:
            platform_id: Platform ID to get ROMs for. If None, gets all ROMs.
            limit: Maximum number of ROMs to return.
            favorites_only: If True, only return ROMs marked as favorites.
            collection_id: Optional collection ID to filter by.
        
        Returns:
            List of ROM dictionaries or dict with 'items' key.
        """
        # Resolve effective collection ID
        effective_collection_id = collection_id
        if favorites_only and not effective_collection_id:
            effective_collection_id = self.get_favorites_collection_id()
            if not effective_collection_id:
                # No favorites collection found - this will cause issues
                return {"items": [], "total": 0, "_no_favorites_collection": True}
        
        # If a collection is being queried, we fetch the whole collection once and cache it
        if effective_collection_id is not None:
            if effective_collection_id not in self._collection_roms_cache:
                print(f"  DEBUG: Fetching ALL ROMs for collection {effective_collection_id} to cache locally...")
                # Use a large limit to ensure we get all items in the collection
                fetch_limit = max(limit, 100000)
                params = {"limit": fetch_limit, "collection_id": effective_collection_id}
                
                result = self._get("/roms", params=params, timeout=180)
                
                if isinstance(result, dict) and "items" in result:
                    items = result["items"]
                elif isinstance(result, list):
                    items = result
                else:
                    items = []
                    
                self._collection_roms_cache[effective_collection_id] = items
                print(f"  DEBUG: Cached {len(items)} ROMs for collection {effective_collection_id}")

            items = self._collection_roms_cache[effective_collection_id]

            # The RomM API sometimes ignores platform_id when querying by collection_id (e.g. for favorites).
            # We perform a client-side filter to ensure we only return ROMs that belong to the requested platform.
            if platform_id is not None:
                filtered_items = []
                schema_has_platform_id = False
                
                for rom in items:
                    rom_plat_id = rom.get("platform_id")
                    if rom_plat_id is None and isinstance(rom.get("platform"), dict):
                        rom_plat_id = rom["platform"].get("id")
                        
                    if rom_plat_id is not None:
                        schema_has_platform_id = True
                        if rom_plat_id == platform_id:
                            filtered_items.append(rom)
                            
                if schema_has_platform_id:
                    print(f"  DEBUG: Filtered {len(filtered_items)} ROMs for platform_id {platform_id} from cache")
                    return {"items": filtered_items, "total": len(filtered_items)}
                else:
                    if items:
                        print("  WARNING: Could not find platform_id in ROM data. Client-side filtering skipped.")
            
            return {"items": items, "total": len(items)}

        # Standard query for platforms (not a collection)
        params = {"limit": limit}
        if platform_id is not None:
            params["platform_id"] = platform_id
            
        print(f"  DEBUG: API query params: {params}")
        result = self._get("/roms", params=params, timeout=180)
        
        if isinstance(result, dict) and "items" in result:
            print(f"  DEBUG: API returned {len(result['items'])} ROMs (total: {result.get('total', 'unknown')})")
        elif isinstance(result, list):
            print(f"  DEBUG: API returned {len(result)} ROMs")
            
        return result

    def get_rom(self, rom_id: int) -> dict:
        """Get detailed info for a specific ROM."""
        return self._get(f"/roms/{rom_id}")

    def get_cover_url(self, rom: dict) -> Optional[str]:
        """Get the cover image URL for a ROM."""
        # Prefer external URL if available (IGDB, etc.)
        if rom.get("url_cover"):
            return rom["url_cover"]
        # Fall back to local server if path exists
        elif rom.get("path_cover_s") or rom.get("path_cover_l"):
            return f"{self.server_url}/api/roms/{rom['id']}/cover/small"
        return None

    def get_screenshot_url(self, rom: dict) -> Optional[str]:
        """Get the screenshot URL for a ROM."""
        # Similar to cover, prefer external URL
        if rom.get("url_screenshot"):
            return rom.get("url_screenshot")
        
        # Fall back to local server paths
        if rom.get("path_screenshot_s"):
            return f"{self.server_url}{rom['path_screenshot_s']}"
        if rom.get("path_screenshot_l"):
            return f"{self.server_url}{rom['path_screenshot_l']}"
        
        return None
    
    def get_rom_download_url(self, rom_id: int) -> str:
        """Get the download URL for a ROM file."""
        return f"{self.server_url}/api/roms/{rom_id}/content/download"
    
    def download_rom_file(self, rom: dict, output_path: Path, show_progress: bool = True) -> bool:
        """Download a ROM file from RomM.
        
        Args:
            rom: ROM dictionary with id and fs_name.
            output_path: Path where the ROM file should be saved.
            show_progress: Whether to show download progress.
        
        Returns:
            True if download succeeded, False otherwise.
        """
        rom_id = rom.get('id')
        fs_name = rom.get('fs_name')
        
        if not rom_id or not fs_name:
            return False
        
        download_url = self.get_rom_download_url(rom_id)
        
        try:
            response = self.session.get(download_url, stream=True, timeout=300)
            response.raise_for_status()
            
            # Get file size if available
            total_size = int(response.headers.get('content-length', 0))
            
            # Write file in chunks with progress
            downloaded = 0
            start_time = time.time()
            chunk_size = 8192
            
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # Show progress every 1MB or at completion
                        if show_progress and total_size > 0 and (downloaded % (1024 * 1024) == 0 or downloaded == total_size):
                            elapsed = time.time() - start_time
                            if elapsed > 0:
                                speed = downloaded / elapsed
                                percent = (downloaded / total_size) * 100
                                print(f"\r      Progress: {percent:.1f}% ({format_bytes(downloaded)}/{format_bytes(total_size)}) @ {format_bytes(speed)}/s", end='', flush=True)
            
            if show_progress and total_size > 0:
                print()  # New line after progress
            
            logging.info(f"Downloaded ROM file: {output_path} ({format_bytes(downloaded)})")
            return True
        except Exception as e:
            if show_progress:
                print()  # New line before error
            print(f"    Error downloading {fs_name}: {e}")
            # Clean up partial download
            if output_path.exists():
                output_path.unlink()
            return False


def format_bytes(bytes_count: int) -> str:
    """Format bytes into human-readable size.
    
    Args:
        bytes_count: Number of bytes.
    
    Returns:
        Formatted string (e.g., "1.5 MB").
    """
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.1f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.1f} TB"


def format_date(date_str: Optional[str]) -> str:
    """Convert date to EmulationStation format (YYYYMMDDTHHMMSS)."""
    if not date_str:
        return ""
    try:
        # Try parsing ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%dT%H%M%S")
    except (ValueError, AttributeError):
        return ""


def create_gamelist_xml(roms: list, retropie_folder: str, rom_base_path: str = None, kid_friendly_rom_ids: set = None, target_config: dict = None) -> ET.Element:
    """Create a gamelist.xml ElementTree from ROM data.
    
    Args:
        roms: List of ROM dictionaries from RomM API.
        platform_slug: RomM platform slug.
        retropie_folder: RetroPie folder name for this platform.
        rom_base_path: Optional base path for ROM files. If None, uses relative paths.
        kid_friendly_rom_ids: Set of ROM IDs that should be marked as kid games.
        target_config: Target system configuration dict (from TARGET_CONFIGS).
    
    Returns:
        ElementTree root element for gamelist.xml.
    """
    root = ET.Element("gameList")
    
    if kid_friendly_rom_ids is None:
        kid_friendly_rom_ids = set()
    
    # Use default RetroPie config if no target specified
    if target_config is None:
        target_config = TARGET_CONFIGS["retropie"]

    for rom in roms:
        game = ET.SubElement(root, "game")

        # Path to the ROM file
        filename = rom.get("fs_name", "") or rom.get("file_name", "")
        rom_name = rom.get("name", "Unknown")
        
        # Use filename if available, otherwise use ROM name
        if not filename:
            filename = rom_name
        
        path_elem = ET.SubElement(game, "path")
        if rom_base_path:
            # Use custom base path (e.g., /romm/library/roms/snes/game.sfc)
            path_elem.text = f"{rom_base_path}/{retropie_folder}/{filename}"
        else:
            # Use relative path (e.g., ./game.sfc)
            path_elem.text = f"./{filename}"

        # Game name
        name_elem = ET.SubElement(game, "name")
        name_elem.text = rom_name

        # Description
        if rom.get("summary"):
            desc_elem = ET.SubElement(game, "desc")
            desc_elem.text = rom.get("summary", "")

        # Cover image
        if rom.get("url_cover") or rom.get("path_cover_s") or rom.get("path_cover_l"):
            image_elem = ET.SubElement(game, "image")
            # Use ROM filename (without extension) for image name to match ES-DE expectations
            rom_filename_base = Path(filename).stem  # Remove extension
            image_filename = f"{rom_filename_base}.png"
            
            # Build image path based on target config
            images_base = os.path.expanduser(target_config["images_path"])
            image_subdir = target_config["image_subdir"]
            
            if image_subdir:
                image_elem.text = f"{images_base}/{retropie_folder}/{image_subdir}/{image_filename}"
            else:
                image_elem.text = f"{images_base}/{retropie_folder}/{image_filename}"

        # Rating (convert from 0-100 to 0-1)
        if rom.get("igdb_metadata", {}).get("total_rating"):
            rating_elem = ET.SubElement(game, "rating")
            rating = float(rom["igdb_metadata"]["total_rating"]) / 100.0
            rating_elem.text = f"{rating:.2f}"

        # Release date
        first_release = rom.get("first_release_date")
        if first_release:
            date_elem = ET.SubElement(game, "releasedate")
            date_elem.text = format_date(first_release)

        # Developer
        if rom.get("igdb_metadata", {}).get("developers"):
            dev_elem = ET.SubElement(game, "developer")
            dev_elem.text = ", ".join(rom["igdb_metadata"]["developers"])

        # Publisher
        if rom.get("igdb_metadata", {}).get("publishers"):
            pub_elem = ET.SubElement(game, "publisher")
            pub_elem.text = ", ".join(rom["igdb_metadata"]["publishers"])

        # Genre
        if rom.get("genres"):
            genre_elem = ET.SubElement(game, "genre")
            genre_elem.text = ", ".join(rom["genres"])

        # Players
        igdb_meta = rom.get("igdb_metadata", {})
        if igdb_meta.get("game_modes"):
            players_elem = ET.SubElement(game, "players")
            modes = igdb_meta["game_modes"]
            if "Multiplayer" in modes or "Co-operative" in modes:
                players_elem.text = "4"
            else:
                players_elem.text = "1"
        
        # Kid game flag
        rom_id = rom.get("id")
        if rom_id and rom_id in kid_friendly_rom_ids:
            kidgame_elem = ET.SubElement(game, "kidgame")
            kidgame_elem.text = "true"

    return root


def prettify_xml(elem: ET.Element) -> str:
    """Return a pretty-printed XML string."""
    rough_string = ET.tostring(elem, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")


def parse_existing_gamelist(gamelist_path: Path) -> dict:
    """Parse existing gamelist.xml to get previously synced games.
    
    Args:
        gamelist_path: Path to the gamelist.xml file.
    
    Returns:
        Dictionary mapping ROM names to their file paths and image paths.
        Format: {rom_name: {'path': str, 'image': str}}
    """
    if not gamelist_path.exists():
        return {}
    
    try:
        tree = ET.parse(gamelist_path)
        root = tree.getroot()
        
        existing_games = {}
        for game in root.findall('game'):
            name_elem = game.find('name')
            path_elem = game.find('path')
            image_elem = game.find('image')
            
            if name_elem is not None and name_elem.text:
                game_info = {}
                if path_elem is not None and path_elem.text:
                    game_info['path'] = path_elem.text
                if image_elem is not None and image_elem.text:
                    game_info['image'] = image_elem.text
                existing_games[name_elem.text] = game_info
        
        return existing_games
    except Exception as e:
        print(f"  Warning: Could not parse existing gamelist.xml: {e}")
        return {}


def remove_unfavorited_games(
    current_roms: list,
    existing_games: dict,
    roms_path: Path,
    images_path: Path,
    dry_run: bool = False
) -> tuple:
    """Remove games that are no longer in the favorites list.
    
    Args:
        current_roms: List of current ROM dictionaries from RomM.
        existing_games: Dictionary of previously synced games from gamelist.xml.
        roms_path: Path to the ROM files directory.
        images_path: Path to the images directory.
        dry_run: If True, only show what would be removed.
    
    Returns:
        Tuple of (removed_roms_count, removed_images_count).
    """
    # Build set of current ROM names
    current_rom_names = {rom.get('name') for rom in current_roms if rom.get('name')}
    
    # Find games that were previously synced but are no longer in current favorites
    removed_games = {name: info for name, info in existing_games.items() 
                     if name not in current_rom_names}
    
    if not removed_games:
        return (0, 0)
    
    print(f"\n  Found {len(removed_games)} game(s) to remove (no longer favorited)")
    
    removed_roms = 0
    removed_images = 0
    
    for game_name, game_info in removed_games.items():
        if dry_run:
            print(f"    [DRY RUN] Would remove: {game_name}")
            continue
        
        print(f"    Removing: {game_name}")
        
        # Remove ROM file
        rom_path_str = game_info.get('path', '')
        if rom_path_str:
            # Extract filename from path (handle both relative ./file.ext and absolute paths)
            rom_filename = Path(rom_path_str).name
            rom_file = roms_path / rom_filename
            if rom_file.exists():
                try:
                    rom_file.unlink()
                    removed_roms += 1
                    logging.info(f"Deleted ROM file: {rom_file}")
                    print(f"      Deleted ROM: {rom_filename}")
                except Exception as e:
                    logging.error(f"Error deleting ROM {rom_file}: {e}")
                    print(f"      Error deleting ROM {rom_filename}: {e}")
        
        # Remove image file
        image_path_str = game_info.get('image', '')
        if image_path_str:
            # Extract filename from path
            image_filename = Path(image_path_str).name
            image_file = images_path / image_filename
            if image_file.exists():
                try:
                    image_file.unlink()
                    removed_images += 1
                    logging.info(f"Deleted image file: {image_file}")
                    print(f"      Deleted image: {image_filename}")
                except Exception as e:
                    logging.error(f"Error deleting image {image_file}: {e}")
                    print(f"      Error deleting image {image_filename}: {e}")
    
    return (removed_roms, removed_images)


def download_image(client: RomMClient, rom: dict, dest_path: Path, max_retries: int = 3) -> bool:
    """Download cover image for a ROM with retry logic."""
    url = client.get_cover_url(rom)
    if not url:
        return False

    for attempt in range(max_retries):
        try:
            # Use session for local server URLs, plain requests for external
            is_local = url.startswith(client.server_url)
            if is_local:
                response = client.session.get(url, timeout=30)
            else:
                response = requests.get(url, timeout=30)
            
            response.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(response.content)
            logging.info(f"Downloaded image: {dest_path}")
            return True
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                # 401 on external URLs means auth required - skip retries
                if not url.startswith(client.server_url):
                    return False
                print(f"  Warning: 401 Auth error for {rom.get('name')} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            elif e.response.status_code == 404:
                # 404 means image doesn't exist - no point retrying
                return False
            elif e.response.status_code == 429:
                print(f"  Warning: Rate limited, waiting...")
                time.sleep(5 * (attempt + 1))
                continue
            else:
                return False
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return False
    
    return False


def sync_folder(
    client: RomMClient,
    retropie_folder: str,
    platforms: list,
    gamelist_base_path: Path,
    download_images: bool = True,
    download_roms: bool = False,
    dry_run: bool = False,
    rom_base_path: str = None,
    favorites_only: bool = False,
    target_config: dict = None,
) -> int:
    """Sync a group of platforms that map to the same RetroPie folder.
    
    Args:
        client: RomM API client.
        retropie_folder: The EmulationStation folder name (e.g. 'n3ds').
        platforms: List of platform dictionaries from RomM that map to this folder.
        # ... [other args remain the same]
    """
    if target_config is None:
        target_config = TARGET_CONFIGS["retropie"]

    platform_path = gamelist_base_path / retropie_folder

    print(f"\n{'='*60}")
    print(f"EmulationStation Folder: {retropie_folder}")
    platform_names = ", ".join([f"{p.get('name', '')} (ID: {p.get('id')})" for p in platforms])
    print(f"  Mapped RomM Platforms: {platform_names}")
    print(f"{'='*60}")

    # 1. Fetch and combine ROMs from ALL platforms mapped to this folder
    all_roms = []
    for platform in platforms:
        try:
            roms_response = client.get_roms(platform["id"], favorites_only=favorites_only)
            
            # Check if no favorites collection was found
            if isinstance(roms_response, dict) and roms_response.get("_no_favorites_collection"):
                print(f"\n{'='*60}")
                print("ERROR: No Favourites collection found!")
                print(f"{'='*60}")
                print("\nYou are trying to sync favorites, but this user account has no")
                print("'Favourites' collection in RomM.")
                print("\nTo fix this:")
                print("  1. Log into RomM with this user account")
                print("  2. Create a collection named 'Favourites' (or 'Favorites')")
                print("  3. Add some ROMs to the collection")
                print("\nOr use --all-roms to sync your entire library instead.")
                print(f"{'='*60}\n")
                return 0
            
            if isinstance(roms_response, dict) and "items" in roms_response:
                all_roms.extend(roms_response["items"])
            elif isinstance(roms_response, list):
                all_roms.extend(roms_response)
        except requests.RequestException as e:
            print(f"  Error fetching ROMs for platform {platform.get('name')}: {e}")

    roms = all_roms

    if not roms:
        if favorites_only:
            print("  No favorite ROMs found for this folder")
        else:
            print("  No ROMs found for this folder")
        return 0

    if favorites_only:
        print(f"  Found {len(roms)} favorite ROMs combined")
    else:
        print(f"  Found {len(roms)} ROMs combined")
    
    if dry_run:
        action_items = []
        if download_roms:
            action_items.append("download ROM files")
        if download_images:
            action_items.append("download images")
        action_items.append("create gamelist.xml")
        
        print(f"\n  [DRY RUN] Would {', '.join(action_items)}")
        for rom in roms[:5]:  # Show first 5
            print(f"    - {rom.get('name', rom.get('file_name', 'Unknown'))}")
        if len(roms) > 5:
            print(f"    ... and {len(roms) - 5} more")
        return len(roms)

    # Create platform directory if needed
    if not platform_path.exists():
        platform_path.mkdir(parents=True, exist_ok=True)
        logging.info(f"Created gamelist directory: {platform_path}")
    
    # Use target-specific paths
    home_dir = Path.home()
    images_base = Path(target_config["images_path"].replace("~", str(home_dir)))
    
    # Override roms_base if rom_base_path is provided
    if rom_base_path:
        rom_base_expanded = Path(rom_base_path.replace("~", str(home_dir)))
        if rom_base_expanded.name == "roms":
            roms_base = rom_base_expanded
        else:
            roms_base = rom_base_expanded / "roms"
    else:
        roms_base = Path(target_config["roms_path"].replace("~", str(home_dir)))
    
    # Build image path with optional subdirectory
    if target_config["image_subdir"]:
        images_path = images_base / retropie_folder / target_config["image_subdir"]
    else:
        images_path = images_base / retropie_folder
    
    roms_path = roms_base / retropie_folder
    gamelist_path = platform_path / "gamelist.xml"
    
    # Parse existing gamelist to track previously synced games
    existing_games = parse_existing_gamelist(gamelist_path)
    if existing_games:
        print(f"  Found {len(existing_games)} previously synced games")
        
        # Cleanup now checks against the COMBINED list of all platforms for this folder
        if favorites_only:
            removed_roms, removed_images = remove_unfavorited_games(
                roms, existing_games, roms_path, images_path, dry_run
            )
            if removed_roms > 0 or removed_images > 0:
                print(f"  Cleanup complete: {removed_roms} ROM(s) and {removed_images} image(s) removed")
                
    if download_images:
        if not images_path.exists():
            images_path.mkdir(parents=True, exist_ok=True)
        print("  Downloading cover images...")
        downloaded = 0
        skipped = 0
        failed = 0
        total_with_covers = sum(1 for rom in roms if rom.get("url_cover") or rom.get("path_cover_s") or rom.get("path_cover_l"))
        
        for i, rom in enumerate(roms):
            has_cover = rom.get("url_cover") or rom.get("path_cover_s") or rom.get("path_cover_l")
            if has_cover:
                rom_filename = rom.get("fs_name", "") or rom.get("file_name", "") or rom.get("name", "unknown")
                rom_filename_base = Path(rom_filename).stem
                image_filename = f"{rom_filename_base}.png"
                image_path = images_path / image_filename
                
                if not image_path.exists():
                    current = downloaded + failed + 1
                    print(f"\r    Downloading image {current}/{total_with_covers}: {rom.get('name', 'Unknown')[:50]}...", end='', flush=True)
                    
                    if download_image(client, rom, image_path):
                        downloaded += 1
                    else:
                        failed += 1
                    
                    if (i + 1) % 10 == 0:
                        time.sleep(0.5)
                else:
                    skipped += 1
        
        if downloaded > 0 or failed > 0:
            print()
        print(f"  Downloaded {downloaded} cover images (skipped {skipped} existing, {failed} failed)")

    if download_roms and not dry_run:
        print(f"\n  Downloading ROM files to: {roms_path}")
        if not roms_path.exists():
            roms_path.mkdir(parents=True, exist_ok=True)
        
        downloaded = 0
        skipped = 0
        failed = 0
        total_roms = len(roms)
        
        for idx, rom in enumerate(roms, 1):
            fs_name = rom.get('fs_name')
            if not fs_name:
                failed += 1
                continue
            
            dest_path = roms_path / fs_name
            if dest_path.exists():
                skipped += 1
                continue
            
            print(f"    [{idx}/{total_roms}] Downloading {fs_name}...")
            if client.download_rom_file(rom, dest_path):
                downloaded += 1
            else:
                failed += 1
        
        print(f"  Downloaded {downloaded} ROM files (skipped {skipped} existing, {failed} failed)")
    
    kid_friendly_rom_ids = client.get_kid_friendly_rom_ids()
    
    print("  Generating gamelist.xml...")
    # NOTE: I removed platform_slug from this function call to match the updated definition above
    gamelist = create_gamelist_xml(roms, retropie_folder, rom_base_path, kid_friendly_rom_ids, target_config)

    xml_content = prettify_xml(gamelist)
    gamelist_path.write_text(xml_content, encoding="utf-8")
    logging.info(f"Created gamelist.xml: {gamelist_path}")
    print(f"  Wrote {gamelist_path}")

    return len(roms)

def detect_esde_paths() -> dict:
    """Detect ROM and media paths from ES-DE settings.
    
    Returns:
        Dict with 'roms_path' and 'media_path' keys, or empty dict if not found.
    """
    esde_settings = Path.home() / "ES-DE" / "settings" / "es_settings.xml"
    
    if not esde_settings.exists():
        print(f"  ES-DE settings not found at: {esde_settings}")
        return {}
    
    result = {}
    
    try:
        content = esde_settings.read_text()
        
        # Look for ROMDirectory
        match = re.search(r'<string name="ROMDirectory" value="([^"]+)"', content)
        if match:
            roms_path = match.group(1)
            print(f"  Found ROMDirectory in ES-DE settings: {roms_path}")
            result['roms_path'] = roms_path
        
        # Look for MediaDirectory
        match = re.search(r'<string name="MediaDirectory" value="([^"]+)"', content)
        if match:
            media_path = match.group(1)
            print(f"  Found MediaDirectory in ES-DE settings: {media_path}")
            result['media_path'] = media_path
        
    except Exception as e:
        print(f"Warning: Could not read ES-DE settings: {e}")
    
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Sync ROM metadata from RomM to EmulationStation (RetroPie, SteamDeck ES-DE, etc.)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync all platforms (RetroPie)
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret

  # Sync for SteamDeck with ES-DE
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --target steamdeck

  # Sync specific platforms only
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --platforms snes,nes,gba

  # Dry run (show what would be done)
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --dry-run

  # Skip image downloads
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --no-images

  # Use absolute ROM paths (shared filesystem with RomM)
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --rom-path /romm/library/roms

  # Sync all ROMs (not just favorites)
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --all-roms

  # Download ROM files along with metadata and images
  python3 romm_sync.py -s https://roms.proveaux.net -u admin -p secret --download-roms
        """,
    )

    parser.add_argument(
        "-s", "--server",
        required=True,
        help="RomM server URL (e.g., https://roms.proveaux.net)",
    )
    parser.add_argument(
        "-u", "--user",
        required=True,
        help="RomM username",
    )
    parser.add_argument(
        "-p", "--password",
        required=True,
        help="RomM password",
    )
    parser.add_argument(
        "--target",
        choices=list(TARGET_CONFIGS.keys()),
        default="retropie",
        help="Target system (default: retropie). Options: " + ", ".join([f"{k} ({v['name']})" for k, v in TARGET_CONFIGS.items()]),
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Gamelist output directory (overrides target default)",
    )
    parser.add_argument(
        "--platforms",
        help="Comma-separated list of platform slugs to sync (default: all)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip downloading cover images",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--list-platforms",
        action="store_true",
        help="List available platforms and exit",
    )
    parser.add_argument(
        "--rom-path",
        help="Base path for Emulation directory (e.g., /run/media/mmcblk0p1/Emulation for SD card). ROMs will be downloaded to {path}/roms/{platform}/. If not specified, uses target default path.",
    )
    parser.add_argument(
        "--all-roms",
        action="store_true",
        help="Sync all ROMs (default: only favorites)",
    )
    parser.add_argument(
        "--download-roms",
        action="store_true",
        help="Download ROM files from RomM server (uses target default path or --rom-path if specified)",
    )
    parser.add_argument(
        "--no-auto-detect",
        action="store_true",
        help="Disable auto-detection of EmuDeck paths and use standard ES-DE default paths",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging()

    # Get target configuration (make a copy so we can modify it)
    target_config = TARGET_CONFIGS[args.target].copy()
    logging.info(f"Target system: {target_config['name']}")
    
    # Auto-detect paths from ES-DE if not disabled
    if not args.no_auto_detect and args.target == "steamdeck":
        detected_paths = detect_esde_paths()
        
        # Use detected ROM path if not specified
        if not args.rom_path and detected_paths.get('roms_path'):
            args.rom_path = detected_paths['roms_path']
            print(f"Auto-detected ES-DE ROM path: {args.rom_path}")
        
        # Use detected media path for images
        if detected_paths.get('media_path'):
            target_config['images_path'] = detected_paths['media_path']
            print(f"Auto-detected ES-DE media path: {detected_paths['media_path']}")
        
        # Validate that required paths were found
        if target_config['images_path'] is None:
            print("\nError: Could not detect MediaDirectory from ES-DE settings.")
            print("Please ensure ES-DE is properly configured, or use --no-auto-detect with manual paths.")
            sys.exit(1)
        
        if target_config['roms_path'] is None and not args.rom_path:
            print("\nError: Could not detect ROMDirectory from ES-DE settings.")
            print("Please ensure ES-DE is properly configured, or specify --rom-path manually.")
            sys.exit(1)
    
    if args.no_auto_detect:
        print("Auto-detection disabled - you must specify paths manually")
        if target_config['images_path'] is None or target_config['roms_path'] is None:
            print("\nError: --no-auto-detect requires manual path configuration.")
            print("The steamdeck target requires ES-DE settings to be detected.")
            print("Either remove --no-auto-detect or use --target retropie with custom paths.")
            sys.exit(1)
    
    # Use target-specific gamelist path if not overridden
    if args.output is None:
        gamelist_output = target_config["gamelist_path"]
    else:
        gamelist_output = args.output

    # Create client
    print(f"Connecting to RomM server: {args.server}")
    client = RomMClient(args.server, args.user, args.password)

    # Test connection and get platforms
    try:
        platforms = client.get_platforms()
    except requests.RequestException as e:
        print(f"Error connecting to RomM server: {e}")
        sys.exit(1)

    print(f"Found {len(platforms)} platforms")
    print("\nPlatform slugs found:")
    for p in platforms:
        print(f"  - {p.get('slug', 'N/A')} ({p.get('name', 'N/A')})")

    # List platforms mode
    if args.list_platforms:
        print("\nAvailable platforms:")
        for p in sorted(platforms, key=lambda x: x.get("name", "")):
            slug = p.get("slug", "")
            name = p.get("name", slug)
            rom_count = p.get("rom_count", 0)
            retropie_folder = PLATFORM_MAP.get(slug, slug)
            print(f"  {slug:30} -> {retropie_folder:20} ({rom_count} ROMs) - {name}")
        sys.exit(0)

    # Filter platforms if specified
    if args.platforms:
        filter_slugs = [s.strip() for s in args.platforms.split(",")]
        platforms = [p for p in platforms if p.get("slug") in filter_slugs]
        if not platforms:
            print(f"No matching platforms found for: {args.platforms}")
            sys.exit(1)

    # Group platforms by their destination EmulationStation folder
    folder_groups = {}
    for platform in platforms:
        slug = platform.get("slug", "")
        folder = PLATFORM_MAP.get(slug, slug)
        if folder not in folder_groups:
            folder_groups[folder] = []
        folder_groups[folder].append(platform)

    # Sync each grouped folder
    gamelist_path = Path(gamelist_output).expanduser()
    total_roms = 0

    favorites_only = not args.all_roms
    
    if favorites_only:
        print("\n** Syncing FAVORITES only (use --all-roms to sync entire library) **\n")
    else:
        print("\n** WARNING: Syncing ALL ROMs (this may take a while) **\n")
    
    for folder_name, mapped_platforms in folder_groups.items():
        count = sync_folder(
            client,
            folder_name,
            mapped_platforms,
            gamelist_path,
            download_images=not args.no_images,
            download_roms=args.download_roms,
            dry_run=args.dry_run,
            rom_base_path=args.rom_path,
            favorites_only=favorites_only,
            target_config=target_config,
        )
        total_roms += count

    print(f"\n{'='*60}")
    print(f"Sync complete! Processed {total_roms} ROMs across {len(folder_groups)} folders")
    if args.dry_run:
        print("(This was a dry run - no files were modified)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()