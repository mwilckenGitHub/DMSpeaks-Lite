"""Project a supported TEI dialogue document into editable corpus records."""

from xml.etree import ElementTree
from xml.sax.saxutils import escape

from tei_inline import BoundedXMLTree, CLOSE_TAG, OPEN_TAGS, STYLES, TEI_NAMESPACE

NS = f'{{{TEI_NAMESPACE}}}'
XML = '{http://www.w3.org/XML/1998/namespace}'
XML_ID = XML + 'id'
XML_BASE = XML + 'base'


def read_document(raw, *, styled_drafts=None):
    """Read dialogue and optionally collect styles for a fresh project import."""
    parser = ElementTree.XMLParser(target=BoundedXMLTree())
    try:
        document = ElementTree.fromstring(raw, parser=parser)
    except (ElementTree.ParseError, LookupError) as error:
        raise ValueError('Could not parse the TEI document as XML.') from error

    if document.tag != NS + 'TEI':
        raise ValueError('Choose a TEI P5 XML document with a TEI root.')
    header, text = _children(document, ('teiHeader', 'text'))
    body, = _children(text, ('body',))
    speakers = _participants(header) if next(body.iter(NS + 'u'), None) is not None else {}
    custom_base = bool(document.get(XML_BASE) or text.get(XML_BASE))
    rows = list(_entries(body, speakers, custom_base))
    if styled_drafts is not None:
        for index, row in enumerate(_entries(body, speakers, custom_base, styled=True)):
            if any(tag in row['text'] for tag in OPEN_TAGS):
                styled_drafts[index] = row['text']
    return rows


def _layout_children(parent):
    if (parent.text or '').strip() or any((child.tail or '').strip() for child in parent):
        raise ValueError('Place TEI dialogue text inside a paragraph or speech.')
    return list(parent)


def _children(parent, names):
    children = _layout_children(parent)
    if [child.tag for child in children] != [NS + name for name in names]:
        raise ValueError('This TEI import supports one header and one text body.')
    return children


def _participants(header):
    speakers = {}
    for group in header.findall(f'{NS}profileDesc/{NS}particDesc'):
        for person in _people(group):
            identifier = person.get(XML_ID)
            if not identifier:
                continue
            if identifier in speakers:
                raise ValueError(f'TEI participant ID {identifier!r} is declared more than once.')
            names = (_participant_name(name) for name in person.findall(NS + 'persName'))
            speakers[identifier] = next((name for name in names if name), '') or (
                person.get('n', '').strip() or identifier)
    return speakers


def _people(group):
    for child in group:
        if child.tag == NS + 'person':
            yield child
        elif child.tag == NS + 'listPerson':
            yield from _people(child)


def _participant_name(name):
    # Structured forenames and surnames may have no intervening XML text.
    # Keep explicit punctuation in mixed-content names.
    if (name.text or '').strip() or any((part.tail or '').strip() for part in name):
        return ' '.join(''.join(name.itertext()).split())
    return ' '.join(word for part in name for word in ''.join(part.itertext()).split())


def _utterance(element, speakers, custom_base, styled):
    row = _entry(element, _inline_text(element, styled=styled, allow_segments=True))
    who = element.get('who')
    if who is None:
        return row
    references = who.split()
    if len(references) != 1 or not references[0].startswith('#') or custom_base:
        raise ValueError('A TEI utterance must name one local participant in @who, or omit @who.')
    identifier = references[0][1:]
    if identifier not in speakers:
        raise ValueError(f'TEI utterance speaker #{identifier} is missing from the header participant list.')
    row.update(npc_id=identifier, npc_name=speakers[identifier])
    return row


def _entries(container, speakers, custom_base, *, styled=False):
    custom_base = custom_base or bool(container.get(XML_BASE))
    for child in _layout_children(container):
        if child.tag == NS + 'div':
            yield from _entries(child, speakers, custom_base, styled=styled)
        elif child.tag == NS + 'p':
            yield _entry(child, _inline_text(child, styled=styled))
        elif child.tag == NS + 'u':
            yield _utterance(child, speakers, custom_base or bool(child.get(XML_BASE)), styled)
        elif child.tag == NS + 'sp':
            parts = _layout_children(child)
            speaker = _inline_text(parts.pop(0)) if parts and parts[0].tag == NS + 'speaker' else ''
            if not parts or any(part.tag != NS + 'p' for part in parts):
                raise ValueError('TEI speeches must contain paragraphs and an optional speaker label.')
            yield _entry(child, '\n\n'.join(_inline_text(part, styled=styled) for part in parts), speaker)
        else:
            raise ValueError('This TEI import supports paragraphs, speeches, utterances, and divisions in the body.')


def _entry(element, text, speaker=''):
    return {'id': element.get(XML_ID) or element.get('n', ''), 'npc_name': speaker, 'text': text}


def _inline_text(element, *, styled=False, allow_segments=False):
    """Keep text and optionally project known styles as canonical inline tags."""
    def text(value):
        return escape(value or '') if styled else value or ''

    parts = [text(element.text)]
    for child in element:
        if child.tag == NS + 'hi':
            content = _inline_text(child, styled=styled, allow_segments=allow_segments)
            rend = child.get('rend', '').split()
            if styled and set(child.attrib) == {'rend'} and rend and all(style in STYLES for style in rend):
                tags = [OPEN_TAGS[index] for index, style in enumerate(STYLES) if style in rend]
                content = ''.join(tags) + content + CLOSE_TAG * len(tags)
            parts.append(content)
        elif child.tag == NS + 'lb' and not child.attrib and len(child) == 0 and not child.text:
            parts.append('\n')
        elif child.tag == NS + 'seg' and allow_segments:
            parts.append(_inline_text(child, styled=styled, allow_segments=True))
        else:
            message = 'This TEI import supports highlighting and plain line breaks inside dialogue.'
            if allow_segments:
                message += ' Utterance segments may contain those same elements.'
            raise ValueError(message)
        parts.append(text(child.tail))
    return ''.join(parts)
