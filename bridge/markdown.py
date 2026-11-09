"""Pure CommonMark display rendering; delivery policy stays in bridge.py."""
from html import escape
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token


class MarkdownError(ValueError):
    pass


_GROUPS = {'paragraph', 'heading', 'blockquote', 'bullet_list', 'ordered_list', 'list_item',
           'strong', 'em', 's', 'link'}
_LEAVES = {'inline', 'text', 'softbreak', 'hardbreak', 'code_inline', 'code_block', 'fence', 'hr', 'image'}
_LANGUAGE = re.compile(r'[A-Za-z0-9_+-]{1,32}\Z')


def _tree(tokens):
    if not isinstance(tokens, list):
        raise MarkdownError('markdown_tokens_invalid')
    root, stack = [], []
    current = root
    for token in tokens:
        if (not isinstance(token, Token) or not isinstance(token.type, str)
                or not isinstance(token.content, str) or not isinstance(token.info, str)
                or not isinstance(token.attrs, dict) or type(token.nesting) is not int):
            raise MarkdownError('markdown_token_invalid')
        kind = token.type
        if kind.endswith('_open') and kind[:-5] in _GROUPS and token.nesting == 1:
            children = []
            current.append((token, children))
            stack.append((kind[:-5], current))
            current = children
        elif kind.endswith('_close') and kind[:-6] in _GROUPS and token.nesting == -1:
            if not stack or stack[-1][0] != kind[:-6]:
                raise MarkdownError('markdown_tokens_unbalanced')
            _, current = stack.pop()
        elif kind in _LEAVES and token.nesting == 0:
            current.append((token, []))
        else:
            raise MarkdownError('markdown_token_unsupported')
    if stack:
        raise MarkdownError('markdown_tokens_unbalanced')
    return root


def _inline(nodes, *, alt=False):
    parts = []
    for token, children in nodes:
        kind = token.type
        if kind == 'text':
            parts.append(escape(token.content, quote=False))
        elif kind in ('softbreak', 'hardbreak'):
            parts.append('\n')
        elif kind == 'code_inline':
            text = escape(token.content, quote=False)
            parts.append(text if alt else '<code>' + text + '</code>')
        elif kind in ('strong_open', 'em_open', 's_open'):
            tag = {'strong_open': 'b', 'em_open': 'i', 's_open': 's'}[kind]
            text = _inline(children, alt=alt)
            parts.append(text if alt else f'<{tag}>{text}</{tag}>')
        elif kind == 'link_open':
            href = token.attrGet('href')
            if not isinstance(href, str) or not href:
                raise MarkdownError('markdown_link_invalid')
            text = _inline(children, alt=alt)
            parts.append(text if alt else '<a href="' + escape(href, quote=True) + '">' + text + '</a>')
        elif kind == 'image':
            src = token.attrGet('src')
            if not isinstance(src, str):
                raise MarkdownError('markdown_image_invalid')
            text = _inline(_tree(token.children), alt=True)
            parts.append(text + ' (' + escape(src, quote=False) + ')')
        else:
            raise MarkdownError('markdown_inline_token_unsupported')
    return ''.join(parts)


def _blocks(nodes):
    parts = []
    for token, children in nodes:
        kind = token.type
        if kind in ('paragraph_open', 'heading_open'):
            if len(children) != 1 or children[0][0].type != 'inline':
                raise MarkdownError('markdown_block_invalid')
            text = _inline(_tree(children[0][0].children))
            parts.append('<b>' + text + '</b>' if kind == 'heading_open' else text)
        elif kind in ('fence', 'code_block'):
            language = token.info.split()[0] if token.info.split() else ''
            cls = f' class="language-{language}"' if _LANGUAGE.fullmatch(language) else ''
            parts.append('<pre><code' + cls + '>' + escape(token.content, quote=False) + '</code></pre>')
        elif kind == 'hr':
            parts.append('---')
        elif kind == 'blockquote_open':
            parts.append('\n'.join('&gt; ' + line for line in _blocks(children).split('\n')))
        elif kind in ('bullet_list_open', 'ordered_list_open'):
            start = token.attrs.get('start', 1)
            if type(start) is not int:
                raise MarkdownError('markdown_list_invalid')
            items = []
            for index, (item, blocks) in enumerate(children):
                if item.type != 'list_item_open':
                    raise MarkdownError('markdown_list_invalid')
                marker = f'{start + index}. ' if kind == 'ordered_list_open' else '• '
                lines = _blocks(blocks).split('\n')
                items.append(marker + lines[0] + ''.join('\n  ' + line for line in lines[1:]))
            parts.append('\n'.join(items))
        else:
            raise MarkdownError('markdown_block_token_unsupported')
    return '\n\n'.join(parts)


def render(source, source_max_units):
    """Render bounded, scalar Unicode input; caller validates HTML and limits."""
    if not isinstance(source, str):
        raise MarkdownError('markdown_source_must_be_text')
    if '\0' in source or any(0xD800 <= ord(c) <= 0xDFFF for c in source):
        raise MarkdownError('markdown_source_invalid_unicode')
    if len(source.encode('utf-16-le')) // 2 > source_max_units:
        raise MarkdownError('markdown_source_too_long')
    parser = MarkdownIt('commonmark', {'html': False, 'linkify': False, 'typographer': False,
                                       'maxNesting': source_max_units}).enable('strikethrough')
    # Parse every destination; bridge.py applies the existing HTML link policy.
    parser.validateLink = lambda _url: True
    try:
        return _blocks(_tree(parser.parse(source)))
    except RecursionError:
        raise MarkdownError('markdown_nesting_too_deep') from None
