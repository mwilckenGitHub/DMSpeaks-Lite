"""Provider compatibility for the portable Lite runtime."""

ENDPOINT = "http://127.0.0.1:8091"


class SharedFishUnavailable(ValueError):
    """The current installation has no managed Fish connection."""


class SharedFishBusy(SharedFishUnavailable):
    """A managed request is waiting for its lease."""


class SharedFish:
    def __init__(self, profile, ident):
        raise SharedFishUnavailable('Choose Fish Audio or a self-hosted Fish server over SSH for this Lite installation.')
