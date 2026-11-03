class ParseError(Exception):
    """Invalid input. Callers catch this type only."""


def parse_age(text):
    if not text.strip().isdigit():
        raise ParseError(f"age must be digits: {text!r}")
    return int(text)
