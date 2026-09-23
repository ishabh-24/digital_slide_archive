"""
Serve the annotation format specification inside the platform.

``docs/SPEC.md`` is the single source of truth for the format. It is rendered
to HTML at request time by a deliberately small Markdown converter that covers
only what the spec uses: ATX headings, paragraphs, fenced code blocks, pipe
tables, bullet and numbered lists, and inline code, bold and links. Anything
else is shown as plain text rather than guessed at.
"""

import html
import os
import re

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')
SPEC_PATH = os.path.join(DOCS_DIR, 'SPEC.md')
EXAMPLE_PATH = os.path.join(DOCS_DIR, 'example-beetle-patient104_wsi1.json')

_INLINE_CODE = re.compile(r'`([^`]+)`')
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_LINK = re.compile(r'\[([^\]]+)\]\(([^)\s]+)\)')
_ORDERED = re.compile(r'^\d+\.\s+')


def _inline(text):
    """Escape a line of text, then apply inline code, bold and links.

    Code spans are protected first so their contents are never re-parsed.
    """
    pieces = []
    last = 0
    for match in _INLINE_CODE.finditer(text):
        pieces.append(_bold_and_links(text[last:match.start()]))
        pieces.append('<code>%s</code>' % html.escape(match.group(1)))
        last = match.end()
    pieces.append(_bold_and_links(text[last:]))
    return ''.join(pieces)


def _bold_and_links(text):
    out = html.escape(text, quote=False)
    out = _BOLD.sub(r'<strong>\1</strong>', out)
    out = _LINK.sub(lambda m: '<a href="%s">%s</a>' % (html.escape(m.group(2), quote=True),
                                                       m.group(1)), out)
    return out


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _table(rows):
    cells = [[c.strip() for c in row.strip().strip('|').split('|')] for row in rows]
    head, body = cells[0], cells[2:]  # cells[1] is the |---| separator
    out = ['<table><thead><tr>']
    out.extend('<th>%s</th>' % _inline(c) for c in head)
    out.append('</tr></thead><tbody>')
    for row in body:
        out.append('<tr>' + ''.join('<td>%s</td>' % _inline(c) for c in row) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def render_markdown(text):
    """Return an HTML fragment for the subset of Markdown described above."""
    lines = text.splitlines()
    out = []
    i = 0
    paragraph = []

    def flush():
        if paragraph:
            out.append('<p>%s</p>' % _inline(' '.join(paragraph)))
            del paragraph[:]

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('```'):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                block.append(lines[i])
                i += 1
            out.append('<pre><code>%s</code></pre>' % html.escape('\n'.join(block)))
            i += 1
            continue

        match = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2)
            out.append('<h%d id="%s">%s</h%d>' % (level, _slug(title), _inline(title), level))
            i += 1
            continue

        if stripped.startswith('|') and i + 1 < len(lines) and re.match(r'^\s*\|?\s*:?-', lines[i + 1]):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue

        if stripped.startswith('- ') or _ORDERED.match(stripped):
            flush()
            ordered = bool(_ORDERED.match(stripped))
            out.append('<ol>' if ordered else '<ul>')
            while i < len(lines):
                current = lines[i].strip()
                is_item = current.startswith('- ') or bool(_ORDERED.match(current))
                if not is_item:
                    break
                item = _ORDERED.sub('', current) if ordered else current[2:]
                i += 1
                # continuation lines are indented and belong to the same item
                while i < len(lines) and lines[i].startswith('  ') and lines[i].strip():
                    item += ' ' + lines[i].strip()
                    i += 1
                out.append('<li>%s</li>' % _inline(item))
            out.append('</ol>' if ordered else '</ul>')
            continue

        if not stripped:
            flush()
            i += 1
            continue

        paragraph.append(stripped)
        i += 1

    flush()
    return '\n'.join(out)


def spec_markdown():
    with open(SPEC_PATH, encoding='utf-8') as handle:
        return handle.read()


def example_json():
    with open(EXAMPLE_PATH, encoding='utf-8') as handle:
        return handle.read()
