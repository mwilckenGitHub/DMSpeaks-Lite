"""Normalized spoken text with Unicode source coordinates; local Qwen draft reviewed."""
from model import spoken_view

def normalized_spoken_spans(text):
    raw_text, raw_spans = spoken_view(text)
    if not raw_text:
        return "", []

    result_chars = []
    result_spans = []
    i = 0
    n = len(raw_text)

    while i < n:
        ch = raw_text[i]
        if ch.isspace():
            start_idx = i
            end_idx = i
            while end_idx < n and raw_text[end_idx].isspace():
                end_idx += 1
            # Union span: first raw whitespace start to last raw whitespace end
            span_start = raw_spans[start_idx][0]
            span_end = raw_spans[end_idx - 1][1]
            result_chars.append(' ')
            result_spans.append((span_start, span_end))
            i = end_idx
        else:
            result_chars.append(ch)
            result_spans.append(raw_spans[i])
            i += 1

    # Strip leading and trailing whitespace
    start = 0
    end = len(result_chars)
    while start < end and result_chars[start] == ' ':
        start += 1
    while end > start and result_chars[end - 1] == ' ':
        end -= 1

    normalized_text = ''.join(result_chars[start:end])
    spans = result_spans[start:end]

    return normalized_text, spans
