"""The Studio UI keeps gaps in source sentence IDs and navigates by sentence order."""
import json

from test_studio_app_node import desk_boot, needs_node, run
from ctb.studio import provider


@needs_node
def test_saved_remove_after_an_earlier_sentence_was_dropped_stays_on_its_source_id():
    p = provider.project("weekly-12")
    p["sentences"] = [dict(s, i=i) for s, i in zip(p["sentences"][1:4], [2, 3, 4])]
    marks = {"2": {"mark": "remove"}}
    data = json.dumps({"ok": True, "project": p, "marks": marks})
    got = run(desk_boot(data=data) + "emit(allEls().filter(function (e) { return e.attrs['data-i']; }).map(function (e) { return [e.attrs['data-i'], e.className]; }));")

    by_id = {i: cls for i, cls in got}
    assert "x" in by_id["2"].split()
    assert "x" not in by_id["3"].split()
    assert "x" not in by_id["4"].split()


@needs_node
def test_player_advances_across_missing_sentence_ids_in_source_order():
    p = provider.project("weekly-12")
    p["sentences"] = [dict(p["sentences"][1], i=2), dict(p["sentences"][4], i=5)]
    p["sentences"][0]["said"] = {"he": "משפט שני"}
    p["sentences"][1]["said"] = {"he": "משפט חמישי"}
    data = json.dumps({"ok": True, "project": p, "marks": {"2": {"mark": "remove"}}})
    got = run(desk_boot(data=data) + "var map = byKeep('map')[0]; map.fire('click', {target: map.childNodes[0]}); "
              "byKeep('player')[0].childNodes.filter(function (e) { return e.className === 'play'; })[0].click(); "
              "timers[timers.length - 1].f(); await flush(); "
              "emit([byKeep('map')[0].childNodes.map(function (e) { return e.className; }), textOf(byKeep('player')[0])]);")
    assert any("cur" in cls.split() for cls in got[0])
    assert "משפט חמישי" in got[1]
