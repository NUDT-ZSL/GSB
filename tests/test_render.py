import asyncio

from pipeline.render import observe_until_ready, parse_list_page
from site_server import render_pages


def test_parse_list_page_container():
    html = render_pages.render_list("alpha", ["BIZ-001", "BIZ-002"], 2, "2")
    model = parse_list_page(html)
    assert model.container_present
    assert model.item_count == 2
    assert model.total_pages == 2
    assert model.next_page == "2"
    assert [i.biz_id for i in model.items] == ["BIZ-001", "BIZ-002"]


def test_parse_list_page_loading():
    model = parse_list_page(render_pages.render_list_loading())
    assert not model.container_present
    assert model.item_count == 0


def test_ready_after_container_and_stable_count():
    sequence = [
        render_pages.render_list_loading(),
        render_pages.render_list("alpha", ["BIZ-001"], 1, ""),
        render_pages.render_list("alpha", ["BIZ-001"], 1, ""),
    ]
    calls = {"n": 0}

    async def fetch():
        html = sequence[min(calls["n"], len(sequence) - 1)]
        calls["n"] += 1
        return html

    result = asyncio.run(observe_until_ready(fetch, max_observations=5, interval=0))
    assert result.ready
    assert result.observations == 3
    assert result.page.item_count == 1


def test_unstable_count_times_out_with_snapshot():
    pages = [
        render_pages.render_list("alpha", ["BIZ-001"], 1, ""),
        render_pages.render_list("alpha", ["BIZ-001", "BIZ-002"], 1, ""),
    ]
    calls = {"n": 0}

    async def fetch():
        html = pages[calls["n"] % 2]
        calls["n"] += 1
        return html

    result = asyncio.run(observe_until_ready(fetch, max_observations=4, interval=0))
    assert not result.ready
    assert result.observations == 4
    assert "items-container" in result.snapshot_html
