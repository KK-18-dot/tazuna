from app.parse import ParseError, parse_age


def main(argv):
    try:
        return parse_age(argv[0])
    except ParseError:
        return None
