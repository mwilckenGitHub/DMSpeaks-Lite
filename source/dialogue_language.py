"""Resolve declared language for the selected entry without guessing from text."""

LANGUAGE_PATHS = (
    ('localization', 'locale'), ('localization', 'language'),
    ('metadata', 'localization', 'locale'), ('metadata', 'localization', 'language'),
    ('dialogue_language',), ('source', 'locale'), ('language',), ('locale',),
    ('metadata', 'dialogue_language'), ('metadata', 'language'), ('metadata', 'locale'),
)


def declared_language(value):
    if not isinstance(value, dict):
        return None
    for path in LANGUAGE_PATHS:
        item = value
        for key in path:
            item = item.get(key) if isinstance(item, dict) else None
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def resolve(entry, corpus, mapped=None):
    """A localized entry wins over the corpus default; alternatives stay separate."""
    localized = declared_language({
        'localization': entry.get('localization'),
        'metadata': {'localization': (entry.get('metadata') or {}).get('localization')}
        if isinstance(entry.get('metadata'), dict) else {},
    })
    if localized:
        return localized
    if isinstance(mapped, str) and mapped.strip():
        return mapped.strip()
    return declared_language(entry) or declared_language(corpus)
