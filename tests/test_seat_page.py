"""Trains with compulsory seat reservation insert a "Rezervácia miesta" page.

Live 2026-10-31, Ex 618 12:53 on 3. 12.: the page preselects "Vybrať
konkrétne miesto" and continuing without a seat gets "Nemáte zvolené
požadované miesta vo vozni". The flow must pick "Nezáleží / Kdekoľvek" and
keep clicking until the personal-data page, however many pages that takes.
"""
import pytest

import zssk_bulk
from zssk_bulk import StepFailed, advance_to_personal, prefer_any_seat


class Radio:
    def __init__(self, page, rid, value, selected):
        self.page, self.rid, self.value, self.selected = page, rid, value, selected

    def is_selected(self):
        return self.selected

    def is_displayed(self):
        return False                         # styled radios: the label is shown

    def get_attribute(self, name):
        return self.rid if name == "id" else None


class Label:
    def __init__(self, radio):
        self.radio = radio

    def is_displayed(self):
        return True

    def click(self):
        page = self.radio.page
        group = self.radio.rid.rsplit(":", 1)[0]    # one radio group per segment
        for r in page.radios:
            if r.rid.rsplit(":", 1)[0] == group:
                r.selected = r is self.radio
        page.rendered += 1                   # AJAX re-render changes the ViewState


class Button:
    def __init__(self, driver):
        self.driver = driver

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.driver.press_continue()


class ErrorBox:
    def __init__(self, text):
        self.text = text

    def is_displayed(self):
        return True

    def get_attribute(self, name):
        return self.text


class Page:
    def __init__(self, name, seat_segments=0, personal=False):
        self.name, self.personal, self.rendered, self.error = name, personal, 0, ""
        self.radios = []
        for seg in range(seat_segments):
            self.radios.append(Radio(self, f"sp:{seg}:0", "NO_PREFERENCE", False))
            self.radios.append(Radio(self, f"sp:{seg}:1", "SEAT_FOR_CHOICE", True))


class Shop:
    """Pages in order; the seat page refuses to advance without NO_PREFERENCE."""

    def __init__(self, pages):
        self.pages, self.index, self.title = pages, 0, ""

    @property
    def page(self):
        return self.pages[self.index]

    @property
    def current_url(self):
        return f"https://predaj.zssk.sk/{self.page.name}"

    def press_continue(self):
        page = self.page
        wanted = [r for r in page.radios if r.value == "SEAT_FOR_CHOICE"]
        if any(r.selected for r in wanted):
            page.error = ("Veľmi nás to mrzí Nemáte zvolené požadované miesta "
                          "vo vozni")
            page.rendered += 1
            return
        self.index += 1

    def find_elements(self, by, value):
        page = self.page
        if value == "input[name='javax.faces.ViewState']":
            return [ErrorBox(f"{page.name}-{page.rendered}")]
        if value == zssk_bulk.NO_PREFERENCE_CSS:
            return [r for r in page.radios if r.value == "NO_PREFERENCE"]
        if value.startswith("label[for="):
            rid = value.split("'")[1]
            return [Label(r) for r in page.radios if r.rid == rid]
        if value == zssk_bulk.PERSONAL_PAGE_CSS:
            return [Button(self)] if page.personal else []
        if "error-box" in value:
            return [ErrorBox(page.error)] if page.error else []
        if (by, value) in zssk_bulk.SELECTORS["continue"]:
            return [] if page.personal else [Button(self)]
        return []

    def execute_script(self, script, *args):
        raise AssertionError("labels are visible, no JS click expected")


def _shop(seat_segments):
    pages = [Page("passenger"), Page("offer")]
    if seat_segments:
        pages.append(Page("position", seat_segments=seat_segments))
    pages += [Page("extras"), Page("cart"), Page("personal", personal=True)]
    return Shop(pages)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(zssk_bulk.time, "sleep", lambda s: None)


def test_usual_four_pages_still_reach_personal():
    shop = _shop(0)
    advance_to_personal(shop)
    assert shop.page.name == "personal"


def test_seat_page_gets_no_preference_and_one_extra_click():
    shop = _shop(1)
    advance_to_personal(shop)
    assert shop.page.name == "personal"
    seat = shop.pages[2]
    assert not seat.error
    assert [r.value for r in seat.radios if r.selected] == ["NO_PREFERENCE"]


def test_every_segment_on_the_seat_page_is_switched():
    shop = _shop(2)
    shop.index = 2
    assert prefer_any_seat(shop) is True
    assert all(r.selected == (r.value == "NO_PREFERENCE") for r in shop.page.radios)
    assert prefer_any_seat(shop) is False        # nothing left to switch


def test_a_seat_page_we_cannot_satisfy_reports_its_name(monkeypatch):
    monkeypatch.setattr(zssk_bulk, "prefer_any_seat", lambda driver: False)
    shop = _shop(1)
    with pytest.raises(StepFailed) as err:
        advance_to_personal(shop)
    assert err.value.step == "position"
    assert "Nemáte zvolené požadované miesta" in str(err.value)


def test_a_shop_that_never_reaches_personal_gives_up():
    shop = Shop([Page(f"p{i}") for i in range(20)])
    with pytest.raises(StepFailed) as err:
        advance_to_personal(shop)
    assert err.value.step == "personal"
