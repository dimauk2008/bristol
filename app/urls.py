from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DATE_SORT_PARAM = "s"
DATE_SORT_VALUE = "104"


def has_date_sort(url: str) -> bool:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    return params.get(DATE_SORT_PARAM) == DATE_SORT_VALUE


def set_date_sort(url: str, enabled: bool) -> str:
    parts = urlsplit(url)
    params = [item for item in parse_qsl(parts.query) if item[0] != DATE_SORT_PARAM]
    if enabled:
        params.append((DATE_SORT_PARAM, DATE_SORT_VALUE))
    query = urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
