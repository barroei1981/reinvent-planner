from agent import keep


def _sessions():
    return {
        "e1": {"code": "GHJ301-S", "status": "enrolled", "scheduled_start": "2026-12-01T14:00:00"},
        "e2": {"code": "IND418-R", "status": "reservable", "scheduled_start": "2026-12-01T16:30:00"},
        "e3": {"code": "OTH100", "status": "reservable", "scheduled_start": "2026-12-02T09:00:00"},
    }


def test_keep_over_declines_and_unkeep_reopens(tmp_path):
    p = tmp_path / "k.json"
    keep.keep("GHJ301-S", ["IND418-R"], path=p)
    assert keep.is_declined("IND418-R", path=p)
    keep.unkeep("GHJ301-S", path=p)
    assert not keep.is_declined("IND418-R", path=p)


def test_conflict_declined_only_on_day_with_kept_session(tmp_path):
    p = tmp_path / "k.json"
    keep.keep("GHJ301-S", path=p)
    newly = keep.decline_conflicts({"e2": "IND418-R", "e3": "OTH100"}, _sessions(), path=p)
    assert newly == [("IND418-R", "GHJ301-S")]
    assert keep.is_declined("IND418-R", path=p) and not keep.is_declined("OTH100", path=p)


def test_conflict_not_declined_when_enrolled_session_was_never_approved(tmp_path):
    p = tmp_path / "k.json"
    assert keep.decline_conflicts({"e2": "IND418-R"}, _sessions(), path=p) == []
    assert not keep.is_declined("IND418-R", path=p)


def test_missing_or_corrupt_state_file_is_empty(tmp_path):
    p = tmp_path / "k.json"
    assert keep.load(p) == {"kept": {}, "declined": {}}
    p.write_text("{not json")
    assert keep.load(p) == {"kept": {}, "declined": {}}
