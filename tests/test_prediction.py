import pytest
from astrbot_plugin_cs_vrs_report.models import DataError, Match, Team
from astrbot_plugin_cs_vrs_report.prediction import history_rows, format_prediction, scheduled_match, format_schedule

NOW=1800000000

def fixture():
    m=Match(50000,Team(1,'A'),Team(2,'B'),0,0,'test',None,'PW',NOW+3600)
    rows=[{'matchId':i+10000,'startTime':(NOW-(i+1)*86400)*1000,'homeScore':s[0],'awayScore':s[1],'eventName':'event'} for i,s in enumerate([(2,1),(2,0),(0,2)])]
    return m,{'code':0,'result':{'teamId':1,'opponentTeamId':2,'matchDTOList':rows}}


def test_h2h_raw_rate_orientation_and_duplicate_filtering():
    match,data=fixture()
    data['result']['matchDTOList']*=2
    rows=history_rows(data,match,NOW)
    assert len(rows)==3
    text=format_prediction(match,rows)
    assert 'A 66.7%｜B 33.3%' in text and '样本：3场' in text
    data['result']['teamId']=2;data['result']['opponentTeamId']=1
    assert 'A 33.3%｜B 66.7%' in format_prediction(match,history_rows(data,match,NOW))


def test_no_future_target_draw_or_out_of_window_leakage():
    match,data=fixture();base=data['result']['matchDTOList'][0]
    data['result']['matchDTOList']=[dict(base,matchId=match.id),dict(base,matchId=6,startTime=(NOW+20)*1000),dict(base,matchId=7,startTime=(NOW-366*86400)*1000),dict(base,matchId=8,homeScore=1,awayScore=1),dict(base,matchId=9,awayScore=None)]
    assert history_rows(data,match,NOW)==[]
    assert '不默认填50%' in format_prediction(match,[])
    data['result']['teamId']=99
    with pytest.raises(DataError):history_rows(data,match,NOW)


def test_conflicting_records_rejected_and_no_history_not_missing():
    match,data=fixture();base=data['result']['matchDTOList'][0]
    data['result']['matchDTOList'].append(dict(base,homeScore=0))
    with pytest.raises(DataError):history_rows(data,match,NOW)
    data['result'].pop('matchDTOList')
    with pytest.raises(DataError):history_rows(data,match,NOW)


def test_scheduled_status_time_tbd_and_schedule_order():
    row={'matchId':50000,'status':2,'team1Id':1,'team2Id':2,'startTime':(NOW+1000)*1000,'team1DTO':{'name':'A'},'team2DTO':{'name':'B'}}
    m=scheduled_match(row,NOW)
    assert m.start_time==NOW+1000 and '/cs预测 2' in format_schedule([m])
    for change in ({'status':3},{'startTime':NOW*1000},{'team2Id':99999},{'team2DTO':{'name':'TBD'}}):
        with pytest.raises(DataError):scheduled_match(dict(row,**change),NOW)
    from astrbot_plugin_cs_vrs_report.match_list import MatchLists
    from astrbot_plugin_cs_vrs_report.models import Ranking
    other=Match(50001,m.team1,m.team2,0,0,'test',None,'PW',NOW+10)
    listed=MatchLists().save('a',[m,other],Ranking([{'id':1,'rank':1}],'PW',NOW),upcoming=True)
    assert listed[0].id==other.id
