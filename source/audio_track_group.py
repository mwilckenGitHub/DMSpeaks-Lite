"""A group of tracks that move and lock together."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioTrackGroup:
    """A resolved group in display order, including its delimiter identity.

    This is a view of the existing arrangement, not a persistent nested-group
    format. An explicit hierarchy requires a separate project migration.
    """

    id: str
    track_ids: tuple[str, ...]

    def __post_init__(self):
        if not self.id or not self.track_ids or len(set(self.track_ids)) != len(self.track_ids):
            raise ValueError('A track group needs an identity and distinct tracks.')
