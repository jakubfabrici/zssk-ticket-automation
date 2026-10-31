from zssk_bulk import pick_connection

ROWS = [
    "09:12\nOs 8809\nKúpiť lístok",
    "07:12\nREX 1810\nKúpiť lístok",
    "16:58\nEX 619 Tatran\nKúpiť lístok",
]


def test_time_only_matches_exact_token():
    assert pick_connection(ROWS, "07:12", None) == 1


def test_train_label_beats_time():
    # Timetable shifted: asked for 07:00 but the labelled service left at 07:12.
    assert pick_connection(ROWS, "07:00", "REX 1810") == 1


def test_train_matching_ignores_case_and_spaces():
    assert pick_connection(ROWS, "16:58", "ex619") == 2
    assert pick_connection(ROWS, "16:58", "EX 619") == 2


def test_train_name_substring_matches():
    assert pick_connection(ROWS, "16:58", "Tatran") == 2


def test_missing_train_falls_back_to_time():
    assert pick_connection(ROWS, "07:12", "IC 44") == 1


def test_no_match_returns_none():
    assert pick_connection(ROWS, "05:00", None) is None
    assert pick_connection(ROWS, "05:00", "IC 44") is None


def test_time_must_be_a_token_not_a_substring():
    assert pick_connection(["107:12x\nweird"], "07:12", None) is None
