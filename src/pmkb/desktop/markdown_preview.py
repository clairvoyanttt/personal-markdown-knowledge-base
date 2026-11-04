"""Offline HTML presentation of the shared parser's source spans."""
import asyncio
import re
from html import escape

from pmkb.application.parsing.document import BlockKind, ProtectedKind
from pmkb.application.parsing.markdown import parse_markdown


class SharedMarkdownPreview:
    async def render(self, context, snapshot, cancel):
        return await asyncio.to_thread(self._render, context, snapshot, cancel)

    def _render(self, context, snapshot, cancel):
        if cancel.is_set():
            return ""
        document = parse_markdown(snapshot.body, context.collection_id)
        inline = [s for s in document.protected_spans if s.kind == ProtectedKind.INLINE_CODE]
        source = snapshot.body.encode('utf-16-le')
        def slice_span(start, length):
            return source[start * 2:(start + length) * 2].decode('utf-16-le')
        index = 0
        parts = []
        for block in document.blocks:
            if cancel.is_set():
                return ''
            start = block.start_utf16
            end = start + block.length_utf16
            raw = slice_span(start, block.length_utf16)
            if block.kind == BlockKind.CODE_FENCE:
                lines = raw.splitlines()
                code = '\n'.join(lines[1:])
                opener = re.match(r'^ {0,3}(`{3,}|~{3,})', lines[0])
                fence = opener.group(1)
                closing = r'^ {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*$'
                if len(lines) > 1 and re.fullmatch(closing, lines[-1]):
                    code = '\n'.join(lines[1:-1])
                parts.append(f'<pre><code>{escape(code)}</code></pre>')
                continue
            chunks = []
            cursor = start
            while index < len(inline) and inline[index].end_utf16 <= start:
                index += 1
            while index < len(inline) and inline[index].start_utf16 < end:
                span = inline[index]
                index += 1
                if cursor <= span.start_utf16 and span.end_utf16 <= end:
                    chunks.append(escape(slice_span(cursor, span.start_utf16 - cursor)))
                    code = slice_span(span.start_utf16, span.length_utf16)
                    chunks.append(f'<code>{escape(code.strip(chr(96)))}</code>')
                    cursor = span.end_utf16
            chunks.append(escape(slice_span(cursor, end - cursor)))
            content = ''.join(chunks).strip()
            if block.kind == BlockKind.HEADING:
                level = block.level or 1
                parts.append(f'<h{level}>{content.lstrip("#").lstrip()}</h{level}>')
            elif block.kind == BlockKind.BLOCK_QUOTE:
                parts.append(f'<blockquote>{content.removeprefix("&gt;").lstrip()}</blockquote>')
            elif block.kind == BlockKind.LIST_ITEM:
                marker = block.marker or '-'
                container = 'ol' if marker[0].isdigit() else 'ul'
                parts.append(f'<{container}><li>{content.removeprefix(escape(marker)).lstrip()}</li></{container}>')
            else:
                parts.append(f'<p>{content}</p>')
        return ''.join(parts)
