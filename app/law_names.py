"""Presentation punctuation is not part of a legal document's identity."""


def clean_title(value):
    if not isinstance(value, str):
        return value
    value = value.strip()
    pairs = {'《': '》', '〈': '〉'}
    while value and value[0] in pairs and value[-1] == pairs[value[0]]:
        opening, closing, depth, encloses_all = value[0], value[-1], 0, True
        for i, char in enumerate(value):
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0 and i != len(value)-1:
                    encloses_all = False
                    break
        if not encloses_all or depth:
            break
        value = value[1:-1].strip()
    return value


def display_title(value):
    return '《' + clean_title(value) + '》'
