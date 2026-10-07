"""`filter` and `filter-lang` parse the same way on every route that takes them."""

from unittest.mock import AsyncMock

import cql2
import orjson
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

EXPRESSION = {"op": "=", "args": [{"property": "id"}, "a"]}
TEXT = "id = 'a'"
JSON = orjson.dumps(EXPRESSION).decode()
NESTED = "Invalid filter parameter: 400: "
TEXT_ERROR = "Invalid filter parameter: expected valid CQL2 text."
ROUTES = ["/search", "/collections", "/aggregate", "/catalogs"]
# The third value holds the routes that answer differently today.
CASES = [
    pytest.param({"filter": TEXT}, EXPRESSION, {}, id="text"),
    pytest.param(
        {"filter": JSON, "filter-lang": "cql2-json"}, EXPRESSION, {}, id="json"
    ),
    pytest.param(
        {"filter": JSON},
        EXPRESSION,
        {"/search": TEXT_ERROR, "/aggregate": TEXT_ERROR},
        id="json-without-filter-lang",
    ),
    pytest.param(
        {"filter": ""},
        None,
        {
            "/collections": NESTED
            + "Error parsing filter: 400: Invalid CQL2-text filter: "
            + "expected valid CQL2 text. Please check your syntax."
        },
        id="empty",
    ),
    pytest.param(
        {"filter": "{", "filter-lang": "cql2-json"},
        "Invalid filter parameter: expected valid CQL2 JSON.",
        {
            "/collections": NESTED
            + "Error parsing filter: unexpected end of data: line 1 column 2 (char 1)",
            "/catalogs": "Invalid filter parameter: "
            + "unexpected end of data: line 1 column 2 (char 1)",
        },
        id="invalid-json",
    ),
    pytest.param(
        {"filter": "id ="},
        TEXT_ERROR,
        {
            "/collections": NESTED
            + "Error parsing filter: 400: Invalid CQL2-text filter: "
            + "expected valid CQL2 text. Please check your syntax.",
            "/catalogs": "Invalid filter parameter: expected valid CQL2 text",
        },
        id="invalid-text",
    ),
    pytest.param(
        {"filter": "[1]", "filter-lang": "cql2-json"},
        "Invalid filter parameter: expected a CQL2 expression with an operator.",
        {
            "/search": "Invalid parameters provided: 1 validation error",
            "/aggregate": "Invalid aggregation parameters.",
            "/catalogs": "Invalid filter parameter: not a CQL2 expression",
        },
        id="not-an-expression",
    ),
]


@pytest_asyncio.fixture
async def http(catalogs_app, catalogs_app_client):
    """A client that returns server errors as responses."""
    async with AsyncClient(
        transport=ASGITransport(app=catalogs_app, raise_app_exceptions=False),
        base_url="http://test-server",
    ) as client:
        yield client


@pytest.fixture
def handed_down(txn_client, monkeypatch):
    """Return the filter that the route hands to the database."""
    database = type(txn_client.database)
    apply = AsyncMock(side_effect=lambda search, cql2_filter: (search, None))
    listing = AsyncMock(return_value=([], None, 0))
    monkeypatch.setattr(database, "apply_cql2_filter", apply)
    monkeypatch.setattr(database, "get_all_collections", listing)
    monkeypatch.setattr(database, "get_all_catalogs", listing)

    def filter_handed_down():
        if listing.await_args:
            return listing.await_args.kwargs["filter"]
        return apply.await_args.args[1] if apply.await_args else None

    return filter_handed_down


async def _get(http, route, params):
    if route == "/aggregate":
        params = {**params, "aggregations": "total_count"}
    return await http.get(route, params=params)


def _check(response, expected, handed_down):
    if isinstance(expected, str):
        assert response.status_code == 400, response.text
        assert response.json()["detail"].startswith(expected), response.text
    else:
        assert response.status_code == 200, response.text
        assert handed_down() == expected


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("params,expected,drift", CASES)
async def test_filter_parameters(http, handed_down, route, params, expected, drift):
    """Each route turns the same parameters into the same filter, or the same 400."""
    expected = drift.get(route, expected)
    _check(await _get(http, route, params), expected, handed_down)


@pytest.mark.parametrize(
    "method,data,expected",
    [
        ("GET", {"filter": TEXT}, EXPRESSION),
        (
            "GET",
            {"filter": TEXT, "filter-lang": "cql-json"},
            NESTED
            + "Only 'cql2-json' and 'cql2-text' filter languages are supported "
            + "for collections. Got 'cql-json'.",
        ),
        ("POST", {"filter": EXPRESSION}, EXPRESSION),
        ("POST", {"filter": TEXT}, EXPRESSION),
        (
            "POST",
            {"filter": EXPRESSION, "filter-lang": "cql-json"},
            NESTED
            + "Only 'cql2-json' and 'cql2-text' filter languages are supported "
            + "for collections. Got 'cql-json'.",
        ),
    ],
)
async def test_collections_search_filter(http, handed_down, method, data, expected):
    """/collections-search takes any filter-lang, and a filter object in POST."""
    if method == "GET":
        response = await http.get("/collections-search", params=data)
    else:
        response = await http.post("/collections-search", json=data)
    _check(response, expected, handed_down)


@pytest.mark.parametrize("route", ROUTES)
async def test_parser_failure_is_a_server_error(http, monkeypatch, route):
    """A failure inside the CQL2 parser is a 500, not a client error."""

    def fail(text):
        raise RuntimeError("parser defect")

    monkeypatch.setattr(cql2, "parse_text", fail)
    response = await _get(http, route, {"filter": TEXT})
    expected = 400 if route in ("/collections", "/catalogs") else 500
    assert response.status_code == expected, response.text
