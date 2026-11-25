"""Shared TEI Inline conventions for editable dialogue and interchange."""

from xml.etree import ElementTree

TEI_NAMESPACE = 'http://www.tei-c.org/ns/1.0'
STYLES = ('bold', 'italic', 'underline')
OPEN_TAGS = tuple(f'<hi rend="{style}">' for style in STYLES)
CLOSE_TAG = '</hi>'


def formatting_rules():
    return [{'source': 'marker', 'match': tag, 'type': 'formatting', 'cue': ''}
            for tag in (*OPEN_TAGS, CLOSE_TAG)]


def formatting_styles(rules):
    """A style is writable only when both tags have formatting rules."""
    markers = {rule['match'] for rule in rules
               if rule['source'] == 'marker' and rule['type'] == 'formatting'}
    return [style for style, tag in zip(STYLES, OPEN_TAGS)
            if CLOSE_TAG in markers and tag in markers]


class BoundedXMLTree(ElementTree.TreeBuilder):
    """Reject DTDs before entity expansion and bound XML tree construction."""

    def __init__(self, *, max_depth=64):
        super().__init__()
        self.depth = 0
        self.nodes = 0
        self.max_depth = max_depth

    def doctype(self, name, public_id, system_id):
        raise ValueError('TEI corpus files must not contain a document type declaration.')

    def start(self, tag, attrs):
        self.depth += 1
        self.nodes += 1
        if self.depth > self.max_depth or self.nodes > 1_000_000:
            raise ValueError('This TEI document exceeds the XML structure limits.')
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)
