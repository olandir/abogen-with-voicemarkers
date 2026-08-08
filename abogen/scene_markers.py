"""
Scene marker module for inserting sound effects into generated audio.

A <<SCENE_MARKER:type>> tag plays a user-supplied sound file at that point
INSIDE a chapter. It marks a scene break - a passage of time, a change of
location, a dream, a memory - without the side effects of the other markers:

- Unlike <<CHAPTER_MARKER:...>> it never starts a new chapter, so audiobook
  navigation is not polluted with one entry per scene.
- Unlike <<VOICE:...>> it never changes the active voice; narration continues
  with whatever voice was speaking before the break.

This module is pure: it parses the mapping table, resolves marker types to
files on disk, and subdivides voice segments. It never reads config and never
touches audio.
"""

import os

from abogen.subtitle_utils import (
    _SCENE_MARKER_PATTERN,
    _SCENE_MARKER_SEARCH_PATTERN,
)

# Sentinel voice name marking a segment as a sound effect rather than TTS text.
# This cannot collide with a real voice: split_text_by_voice_markers only ever
# emits a name from VOICES_INTERNAL or a formula like "af_heart*0.5 + am_echo*0.5".
SFX_SEGMENT_VOICE = "__ABOGEN_SFX__"

# Probed in this order when falling back to the SFX folder.
SUPPORTED_SFX_EXTENSIONS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


def is_sfx_segment(voice_name):
    """True if this segment is a sound effect insertion rather than TTS text."""
    return voice_name == SFX_SEGMENT_VOICE


def parse_scene_markers_list(mappings_str):
    """
    Parse newline-separated "type|path" or "type|path|gain_db" format.

    Type names are lowercased so <<SCENE_MARKER:Time>> and <<SCENE_MARKER:time>>
    resolve identically. Later lines win on duplicate types. Blank lines, lines
    starting with '#', and lines without a '|' are skipped.

    Args:
        mappings_str: String with one mapping per line

    Returns:
        Dict of {marker_type: (path, gain_db)}
    """
    mappings = {}
    if not mappings_str:
        return mappings

    for line in mappings_str.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue

        # Windows paths cannot contain '|', so splitting on every pipe is safe
        parts = line.split("|")
        marker_type = parts[0].strip().lower()
        path = parts[1].strip()

        gain_db = 0.0
        if len(parts) > 2 and parts[2].strip():
            try:
                gain_db = float(parts[2].strip())
            except ValueError:
                # An unparseable gain is not fatal - fall back to 0 dB
                gain_db = 0.0

        if marker_type and path:
            mappings[marker_type] = (os.path.expanduser(path), gain_db)

    return mappings


def resolve_scene_marker(marker_type, mappings, sfx_folder=""):
    """
    Resolve a marker type to a sound file on disk.

    Explicit mappings always win. A mapping that points at a nonexistent file is
    reported as "mapping_missing" and does NOT silently fall through to the
    folder, so a typo in the mapping table stays visible instead of being masked
    by an unrelated file that happens to share the name.

    Args:
        marker_type: Type from <<SCENE_MARKER:type>>
        mappings: Dict from parse_scene_markers_list
        sfx_folder: Optional folder searched as a fallback

    Returns:
        Tuple of (path, gain_db, source):
            - path: Absolute path, or None if unresolved
            - gain_db: Gain from the mapping, 0.0 for folder hits
            - source: "mapping", "folder", "mapping_missing", or "unmapped"
    """
    key = (marker_type or "").strip().lower()
    if not key:
        return None, 0.0, "unmapped"

    folder = os.path.expanduser(sfx_folder.strip()) if sfx_folder else ""

    if key in mappings:
        path, gain_db = mappings[key]
        # Relative mapping paths resolve against the SFX folder, since the GUI's
        # working directory is not something the user controls.
        if not os.path.isabs(path) and folder:
            path = os.path.join(folder, path)
        if os.path.isfile(path):
            return os.path.abspath(path), gain_db, "mapping"
        return None, gain_db, "mapping_missing"

    if folder and os.path.isdir(folder):
        # Fast path: exact lowercase name with a known extension
        for ext in SUPPORTED_SFX_EXTENSIONS:
            candidate = os.path.join(folder, key + ext)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate), 0.0, "folder"

        # Slow path: case-insensitive scan, so "Time.WAV" is found on
        # case-sensitive filesystems. On Windows the fast path always wins.
        try:
            for name in sorted(os.listdir(folder)):
                stem, ext = os.path.splitext(name)
                if stem.lower() == key and ext.lower() in SUPPORTED_SFX_EXTENSIONS:
                    candidate = os.path.join(folder, name)
                    if os.path.isfile(candidate):
                        return os.path.abspath(candidate), 0.0, "folder"
        except OSError:
            pass

    return None, 0.0, "unmapped"


def build_resolution_table(marker_types, mappings_str, sfx_folder=""):
    """
    Resolve every distinct marker type once, for logging and for playback.

    Resolving up front means a file cannot be reported as found at log time and
    then be missing at playback time within the same run.

    Args:
        marker_types: Iterable of type strings as they appeared in the text
        mappings_str: Raw mapping list from config
        sfx_folder: Optional fallback folder

    Returns:
        Dict of {marker_type: (path_or_None, gain_db, source)}
    """
    mappings = parse_scene_markers_list(mappings_str)
    table = {}
    for marker_type in marker_types:
        key = (marker_type or "").strip().lower()
        if key and key not in table:
            table[key] = resolve_scene_marker(key, mappings, sfx_folder)
    return table


def validate_scene_markers(mappings_str, sfx_folder=""):
    """
    Check every configured mapping, plus everything discoverable in the SFX
    folder, without needing any input text.

    Args:
        mappings_str: Raw mapping list from config
        sfx_folder: Optional fallback folder

    Returns:
        List of (marker_type, status, detail) where status is:
            - "ok": explicit mapping resolves to an existing file
            - "missing": explicit mapping points at a nonexistent file
            - "folder": discovered only via the SFX folder
    """
    mappings = parse_scene_markers_list(mappings_str)
    rows = []

    for marker_type in sorted(mappings):
        path, gain_db, source = resolve_scene_marker(marker_type, mappings, sfx_folder)
        if source == "mapping":
            detail = path if gain_db == 0.0 else f"{path}  ({gain_db:+.1f} dB)"
            rows.append((marker_type, "ok", detail))
        else:
            rows.append(
                (
                    marker_type,
                    "missing",
                    f"{mappings[marker_type][0]}  (file not found)",
                )
            )

    folder = os.path.expanduser(sfx_folder.strip()) if sfx_folder else ""
    if folder and os.path.isdir(folder):
        try:
            for name in sorted(os.listdir(folder)):
                stem, ext = os.path.splitext(name)
                key = stem.lower()
                if ext.lower() in SUPPORTED_SFX_EXTENSIONS and key not in mappings:
                    rows.append((key, "folder", os.path.join(folder, name)))
        except OSError as e:
            rows.append(("", "missing", f"Cannot read SFX folder: {e}"))

    return rows


def get_first_marker_type(mappings_str):
    """Return the first configured marker type, or None. Used to prefill the GUI
    insert button with something that will actually resolve."""
    for line in (mappings_str or "").strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        marker_type = line.split("|")[0].strip().lower()
        if marker_type:
            return marker_type
    return None


def format_marker(marker_type):
    """Build the literal tag text for a marker type."""
    return f"<<SCENE_MARKER:{marker_type}>>"


def expand_scene_markers(voice_segments, enabled=True):
    """
    Subdivide (voice, text) segments on <<SCENE_MARKER:type>> boundaries.

    Runs AFTER split_text_by_voice_markers so the voice surrounding a marker is
    already resolved and gets carried onto the text on both sides of the break.

    IMPORTANT: this must run even when the feature is disabled. Otherwise the
    marker text survives into the TTS input and Kokoro reads it aloud.

    Args:
        voice_segments: List of (voice_name, text) tuples
        enabled: When False the markers are merely stripped from the text and no
                 sound effect segments are produced.

    Returns:
        Tuple of (segments, marker_types):
            - segments: List of (voice_name, text) tuples where sound effect
              entries are (SFX_SEGMENT_VOICE, lowercased_type)
            - marker_types: Ordered list of every type encountered, including
              duplicates and "" for malformed markers, for logging
    """
    expanded = []
    marker_types = []

    for voice_name, segment_text in voice_segments:
        # Never re-split an already-expanded list
        if is_sfx_segment(voice_name):
            expanded.append((voice_name, segment_text))
            continue

        matches = list(_SCENE_MARKER_SEARCH_PATTERN.finditer(segment_text))

        if not matches:
            if segment_text.strip():
                expanded.append((voice_name, segment_text))
            continue

        if not enabled:
            stripped = _SCENE_MARKER_PATTERN.sub("", segment_text).strip()
            if stripped:
                expanded.append((voice_name, stripped))
            continue

        last_end = 0
        for match in matches:
            before = segment_text[last_end : match.start()].strip()
            if before:
                expanded.append((voice_name, before))

            marker_type = match.group(1).strip()
            marker_types.append(marker_type)
            if marker_type:
                expanded.append((SFX_SEGMENT_VOICE, marker_type.lower()))
            # An empty type is a malformed <<SCENE_MARKER:>>. It is recorded for
            # the warning log but produces no segment at all.

            last_end = match.end()

        tail = segment_text[last_end:].strip()
        if tail:
            expanded.append((voice_name, tail))

    return expanded, marker_types
