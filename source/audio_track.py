"""Track identity and editing state shared by arrangement controls."""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class AudioTrack:
    """The stable identity and group-lock state of one existing audio track.

    Audio settings and clips remain in the arrangement until the full track
    model and project migration are ready; this view does not copy their data.
    """

    id: str
    group_locked: bool = False

    @classmethod
    def from_mapping(cls, track: Mapping):
        ident = track.get('id')
        locked = track.get('group_locked', False)
        if not isinstance(ident, str) or not ident:
            raise ValueError('Each track needs an identifier.')
        if type(locked) is not bool:
            raise ValueError('Group lock must be on or off.')
        return cls(ident, locked)
