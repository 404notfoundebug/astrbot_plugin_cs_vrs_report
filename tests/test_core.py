import asyncio
import copy
import json
import pathlib
import time

import pytest

from astrbot_plugin_cs_vrs_report.models import AccessBlocked, DataError, Player, format_report
from astrbot_plugin_cs_vrs_report.sources import Sources, parse_hltv_players, parse_pw_matches, parse_pw_ranking, parse_pw_report
from astrbot_plugin_cs_vrs_report.engine import Engine, valid_umo
from astrbot_plugin_cs_vrs_report.storage import Store

FIXTURES=pathlib.Path(__file__).parent/'fixtures'
def fixture(name):return json.loads((FIXTURES/name).read_text())
def run(task):return asyncio.run(task)


def test_real_vrs_not_hltv_points_ranking():
    rank=parse_pw_ranking(fixture('pw_vrs.json'))
    assert len(rank.teams)==10 and rank.teams[0]['id']==9565 and rank.teams[0]['points']==1986
    assert rank.rank_of(7020)==5 and rank.rank_of(4608) is None
    data=fixture('pw_vrs.json');data['code']=1
    with pytest.raises(DataError):parse_pw_ranking(data)


def test_incomplete_or_duplicate_ranking_stops_filtering():
    data=fixture('pw_vrs.json');data['result']=data['result'][:9]
    with pytest.raises(DataError):parse_pw_ranking(data)
    data=fixture('pw_vrs.json');data['result'][1]['hltvId']=data['result'][0]['hltvId']
    with pytest.raises(DataError):parse_pw_ranking(data)


def test_only_finished_matches_and_either_side_top_ten():
    matches=parse_pw_matches(fixture('pw_matches.json'))
    assert all(m.id not in (2399752,2399753,2399754) for m in matches)
    report=parse_pw_report(fixture('pw_detail.json'))
    assert report.match.relevant(parse_pw_ranking(fixture('pw_vrs.json')))
    assert report.match.team2.id==4608  # NAVI outside top ten, Spirit inside.


def test_real_series_stats_and_rating_values():
    report=parse_pw_report(fixture('pw_detail.json'))
    assert report.complete() and len(report.players)==10
    assert (report.match.score1,report.match.score2)==(1,2)
    assert [(m['score1'],m['score2']) for m in report.maps]==[(2,13),(13,10),(3,13)]
    p=next(p for p in report.players if p.name=='magixx')
    assert (p.kills,p.deaths,p.rating)==(34,45,1.0) and p.kd=='0.76'
    p=next(p for p in report.players if p.name=='makazze')
    assert (p.kills,p.deaths,p.rating)==(57,37,1.63)
    assert 'Rating（完美世界原站）' in format_report(report)


def test_map_selection_is_not_aggregate_or_ct_t():
    data=fixture('pw_detail.json')
    all_report=parse_pw_report(data)
    one=parse_pw_report(data,1)
    assert one.scope=='第 1 图 · cache' and one.complete()
    assert next(p for p in one.players if p.name=='donk').kills==9
    assert next(p for p in all_report.players if p.name=='donk').kills==31
    with pytest.raises(DataError):parse_pw_report(data,7)
    data['result']['match']['statsDTOList']=data['result']['match']['statsDTOList'][1:]
    missing=parse_pw_report(data)
    assert not missing.players and not missing.complete()  # No averaging per-map ratings.


def test_missing_nan_and_zero_deaths_are_not_fabricated():
    p=Player(1,'p',1,10,0,1.2)
    assert p.kd=='∞'
    p.kills=0;assert p.kd=='—'
    data=fixture('pw_detail.json')
    data['result']['match']['statsDTOList'][0]['team1PlayerStatsDTOList'][0]['rating']='NaN'
    assert not parse_pw_report(data).complete()


def test_hltv_headers_and_scope_guard():
    match=parse_pw_report(fixture('pw_detail.json')).match
    tables=[]
    for team in [match.team1,match.team2]:
        rows=''.join(f'<tr><td><a href="/stats/players/{team.id+i}/p">p{i}</a></td><td>{10+i}-8</td><td>1.20</td></tr>' for i in range(5))
        tables.append(f'<table class="stats-table totalstats"><thead><tr><th>{team.name}</th><th>K-D</th><th>Rating 3.0</th></tr></thead><tbody>{rows}</tbody></table>')
    players,label=parse_hltv_players(''.join(tables),match)
    assert len(players)==10 and '3.0' in label
    with pytest.raises(DataError):parse_hltv_players(''.join(tables).replace('totalstats','ctstats'),match)


class FakeProvider:
    def __init__(self,name,result=None,error=None):self.name=name;self.result=result;self.error=error;self.calls=0
    async def ranking(self):
        self.calls+=1
        if self.error:raise self.error
        return self.result


def test_perfect_world_priority_and_blocked_source_backoff():
    async def test():
        rank=parse_pw_ranking(fixture('pw_vrs.json'));source=Sources()
        assert source.providers[0].name=='perfectworld'
        pw=FakeProvider('perfectworld',rank);hltv=FakeProvider('hltv',rank)
        source.providers=[pw,hltv]
        await source.call('ranking');assert pw.calls==1 and hltv.calls==0
        pw.error=AccessBlocked('blocked')
        await source.call('ranking');await source.call('ranking')
        assert pw.calls==2 and hltv.calls==2
    run(test())


class FakeSources:
    mode='auto'
    def __init__(self):
        self.blocked={};self.rows=[];self.calls=0;self.fail_stats=False;self.fail_listing=False
        self.rank=parse_pw_ranking(fixture('pw_vrs.json'))
    async def ranking(self):return self.rank
    async def results(self,rank):
        if self.fail_listing:raise DataError('upstream unavailable')
        return self.rows
    async def report(self,mid,map_index=0):
        self.calls+=1
        report=parse_pw_report(fixture('pw_detail.json'));report.match.id=mid
        if self.fail_stats:report.players=[]
        return report


def setup_engine(tmp_path,send_result=True):
    store=Store(tmp_path/'db.sqlite');source=FakeSources();sent=[]
    async def send(umo,text):
        sent.append((umo,text))
        if isinstance(send_result,Exception):raise send_result
        return send_result
    async def review(report,umo):return '数据点评测试'
    engine=Engine(store,source,send,review,{'settle_minutes':0,'stats_wait_hours':6})
    return engine,store,source,sent


def fresh_match(mid=2399751):
    m=parse_pw_report(fixture('pw_detail.json')).match;m.id=mid;m.end_time=time.time()-600
    return m


def test_new_target_baseline_no_history_then_exactly_one_ack(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path);umo='qq-snowluma:GroupMessage:123'
        source.rows=[fresh_match()];await engine.scan([umo]);assert sent==[]
        source.rows.append(fresh_match(2399755));await engine.scan([umo]);await engine.scan([umo])
        assert len(sent)==1 and store.counts()['sent']==1
        assert '57-37' in sent[0][1]
        second='qq-snowluma:GroupMessage:456';await engine.scan([umo,second]);assert len(sent)==1
        store.close()
    run(test())


def test_no_umo_no_network_and_reject_private_umo(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path);source.fail_listing=True
        await engine.scan([]);assert not sent and not engine.last_check
        assert valid_umo('qq-snowluma:GroupMessage:123')
        assert not valid_umo('qq-snowluma:FriendMessage:123')
        with pytest.raises(DataError):await engine.scan(['123'])
        store.close()
    run(test())


def test_statistics_delayed_no_send_no_bad_cache(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path);umo='qq-snowluma:GroupMessage:123'
        await engine.scan([umo]);source.rows=[fresh_match()];source.fail_stats=True
        await engine.scan([umo]);assert not sent and store.counts()['pending']==1
        source.fail_stats=False;store.db.execute('UPDATE jobs SET retry_at=0');store.db.commit()
        await engine.scan([umo]);assert len(sent)==1 and source.calls==2
        store.close()
    run(test())


def test_uncertain_send_is_held_until_explicit_retry(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path,RuntimeError('network reset'));umo='qq-snowluma:GroupMessage:123'
        await engine.scan([umo]);source.rows=[fresh_match()];await engine.scan([umo]);await engine.scan([umo])
        assert len(sent)==1 and store.counts()['uncertain']==1
        assert store.retry_uncertain(umo,2399751)
        store.close()
    run(test())


def test_no_platform_false_is_not_marked_sent(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path,False);umo='qq-snowluma:GroupMessage:123'
        await engine.scan([umo]);source.rows=[fresh_match()];await engine.scan([umo])
        assert store.counts().get('sent',0)==0 and store.counts()['pending']==1
        store.close()
    run(test())


def test_restart_mid_send_becomes_uncertain(tmp_path):
    store=Store(tmp_path/'db.sqlite');umo='qq-snowluma:GroupMessage:123'
    store.add(umo,fresh_match(),parse_pw_ranking(fixture('pw_vrs.json')),'sending',time.time());store.close()
    store=Store(tmp_path/'db.sqlite');assert store.counts()['uncertain']==1;store.close()


def test_stale_results_and_non_top_ten_not_published(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path);umo='qq-snowluma:GroupMessage:123';await engine.scan([umo])
        old=fresh_match();old.end_time=time.time()-90000
        other=fresh_match(2399999);other.team1.id=888888;other.team2.id=999999
        source.rows=[old,other];await engine.scan([umo])
        assert not sent and store.counts()['expired']==1 and store.counts()['ineligible']==1
        store.close()
    run(test())


def test_upstream_failure_does_not_initialize_subscription(tmp_path):
    async def test():
        engine,store,source,sent=setup_engine(tmp_path);umo='qq-snowluma:GroupMessage:123';source.fail_listing=True
        with pytest.raises(DataError):await engine.scan([umo])
        source.fail_listing=False;source.rows=[fresh_match()];await engine.scan([umo])
        assert not sent and store.counts()['baseline']==1
        store.close()
    run(test())


def test_top_fifteen_display_does_not_expand_top_ten_filter():
    data=fixture('pw_vrs.json')
    for position in range(11,16):
        data['result'].append({'rank':position,'hltvId':900000+position,'teamName':f'Synthetic{position}','score':1500-position})
    display=parse_pw_ranking(data,limit=15)
    filtering=parse_pw_ranking(data)
    assert len(display.teams)==15 and display.rank_of(900015)==15
    assert len(filtering.teams)==10 and filtering.rank_of(900011) is None
    assert display.teams[-1]['points']==1485
    data['result'].pop()
    with pytest.raises(DataError):parse_pw_ranking(data,limit=15)
