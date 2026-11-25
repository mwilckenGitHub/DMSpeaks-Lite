"""Find the registered voice samples used by one portable project.

The machine-wide reference library is not itself a project resource. Start with
corpus assignments and follow the saved audio's provenance. Shared samples are
included when used; unrelated samples stay in the local library.
"""
import copy

from audio_sources import registry


class ProjectReferences:
    """Collect references incrementally as the export walks audio dependencies."""

    def __init__(self, store, dataset, workspace):
        self._references = store.setting('references', []) or []
        self._by_id = {reference['id']: reference for reference in self._references}
        self._selected = set()

        for source in registry(store, dataset)['sources']:
            self._include(source['id'])
        if workspace.get('dataset') == dataset:
            self._include(workspace.get('reference_id'))

    def _include(self, ident):
        # Declared-only corpus samples are bundled separately by
        # portable_references. Missing ancestors are provenance, not permission
        # to substitute another voice or to import the entire local library.
        while isinstance(ident, str) and ident in self._by_id:
            if ident in self._selected:
                break
            self._selected.add(ident)
            ident = self._by_id[ident].get('parent_reference_id')

    def include_metadata(self, item):
        """Read application-owned reference fields, not arbitrary user metadata."""
        self._include(item.get('reference_id'))
        for key in ('generation', 'source_generation'):
            trace = item.get(key)
            if isinstance(trace, dict) and isinstance(trace.get('request'), dict):
                self._include(trace['request'].get('reference_id'))
        for key in ('provider', 'source_provider', 'provenance'):
            provenance = item.get(key)
            if isinstance(provenance, dict):
                self._include(provenance.get('reference_id'))

    def selected(self):
        """Return independent copies in library order for stable serialization."""
        return [copy.deepcopy(reference) for reference in self._references
                if reference['id'] in self._selected]
