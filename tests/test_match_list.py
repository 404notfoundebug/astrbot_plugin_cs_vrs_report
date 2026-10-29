import pytest
from astrbot_plugin_cs_vrs_report.match_list import MatchLists, format_list
from astrbot_plugin_cs_vrs_report.models import DataError, Match, Ranking, Team


def match(mid, stamp, top=True):
    return Match(mid,Team(1 if top else 20,'Spirit'),Team(30,'NAVI'),2,1,'Test',stamp,'perfectworld')


def test_snapshot_stays_stable_and_is_scoped_to_conversation():
    rank=Ranking([{'id':1,'rank':1}], 'test', 1)
    lists=MatchLists()
    matches=lists.save('groupA',[match(10,10),match(20,20),match(30,30,False),match(20,20)],rank)
    assert [m.id for m in matches]==[20,10]
    assert lists.select('groupA',2)[0].id==10
    lists.save('groupB',[match(40,40)],rank)
    assert lists.select('groupA',2)[0].id==10
    with pytest.raises(DataError):lists.select('groupC',1)
    for value in (0,-1,3):
        with pytest.raises(DataError):lists.select('groupA',value)
    text=format_list(matches)
    assert '1. Spirit 2:1 NAVI' in text and '/cs 2' in text


def test_snapshot_expiration_replacement_and_bound(monkeypatch):
    import astrbot_plugin_cs_vrs_report.match_list as module
    clock=[1000]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    rank=Ranking([{'id':1,'rank':1}], 'test', 1)
    lists=MatchLists(ttl=30,limit=2)
    lists.save('a',[match(1,1)],rank)
    lists.save('a',[match(2,2)],rank)
    assert lists.select('a',1)[0].id==2
    lists.save('b',[match(3,3)],rank)
    lists.save('c',[match(4,4)],rank)
    assert len(lists.items)==2
    with pytest.raises(DataError):lists.select('a',1)
    clock[0]+=30
    with pytest.raises(DataError):lists.select('c',1)
    lists.save('d',[match(i,i) for i in range(30)],rank)
    assert len(lists.items)==1 and len(lists.items['d'][1])==20
    assert '没有' in format_list([])
