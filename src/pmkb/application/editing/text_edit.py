"""Map Qt's paragraph representation back to unchanged Markdown source spans."""


def apply_widget_text(original: str, raw_widget_text: str) -> str:
    # toRawText retains NBSP and line separators, but paragraph boundaries become
    # U+2029. Preserve the original spelling of every unaffected source span.
    before, boundaries = [], [0]
    i = 0
    while i < len(original):
        char = original[i]
        i += 1
        if char == '\r':
            if i < len(original) and original[i] == '\n':
                i += 1
            char = '\n'
        elif char == '\u2029':
            char = '\n'
        before.append(char)
        boundaries.append(i)
    before = ''.join(before)
    after = raw_widget_text.replace('\u2029', '\n')
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(before), len(after)) - prefix
           and before[-1 - suffix] == after[-1 - suffix]):
        suffix += 1
    stop = len(after) - suffix
    return (original[:boundaries[prefix]] + after[prefix:stop]
            + original[boundaries[len(before) - suffix]:])
