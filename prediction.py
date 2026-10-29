"""Historical head-to-head frequencies, not calibrated future probabilities."""
import time
from datetime import datetime
from .models import CST, DataError, Match, Team, clean, integer, epoch


def scheduled_match(row, now=None):
    now=time.time() if now is None else now
    if integer(row.get('status')) not in (1,2):
        raise DataError('仅支持尚未开始的比赛。')
    stamp=epoch(row.get('startTime'))
    a=integer(row.get('team1Id'));b=integer(row.get('team2Id'));mid=integer(row.get('matchId'))
    first=row.get('team1DTO') or {};second=row.get('team2DTO') or {}
    if not stamp or stamp<=now:raise DataError('比赛已经开始，或开赛时间尚未确定。')
    if not a or not b or not mid or a==b or a in (99998,99999) or b in (99998,99999):
        raise DataError('比赛双方尚未确定。')
    if any(clean(t.get('name')).upper() in ('','TBD','TBA') for t in (first,second)):
        raise DataError('比赛双方尚未确定。')
    return Match(mid,Team(a,clean(first.get('name'))),Team(b,clean(second.get('name'))),0,0,
                 clean((row.get('csgoEventDTO') or {}).get('nameZh') or (row.get('csgoEventDTO') or {}).get('name') or 'CS2赛事'),
                 None,'完美世界赛事数据中心',stamp)


def history_rows(data,match,now=None):
    now=time.time() if now is None else now
    result=data.get('result')
    if data.get('code')!=0 or not isinstance(result,dict):raise DataError('历史交锋数据结构异常。')
    a=integer(result.get('teamId'));b=integer(result.get('opponentTeamId'))
    if {a,b}!={match.team1.id,match.team2.id}:raise DataError('历史交锋队伍与预测比赛不一致。')
    rows=result.get('matchDTOList')
    if not isinstance(rows,list):raise DataError('历史交锋列表缺失。')
    valid={};cutoff=min(now,match.start_time)
    for row in rows:
        mid=integer(row.get('matchId'));stamp=epoch(row.get('startTime'))
        s1=integer(row.get('homeScore'));s2=integer(row.get('awayScore'))
        if not mid or mid==match.id or not stamp or not cutoff-365*86400<=stamp<cutoff:
            continue
        if s1 is None or s2 is None or s1==s2:continue
        if a!=match.team1.id:s1,s2=s2,s1
        value=(mid,stamp,s1,s2,clean(row.get('eventName')))
        if mid in valid and valid[mid]!=value:raise DataError('历史交锋同场记录冲突，暂停计算。')
        valid[mid]=value
    return sorted(valid.values(),key=lambda r:r[1],reverse=True)


def format_prediction(match,rows):
    a=match.team1.name;b=match.team2.name;n=len(rows)
    lines=[f'赛前参考｜{a} vs {b}',datetime.fromtimestamp(match.start_time,CST).strftime('开赛：%m-%d %H:%M（北京时间）')+' · '+match.event]
    if not n:
        lines.append('近365天没有可用的两队已决胜负交锋记录，无法计算胜率；不默认填50%。')
    else:
        wins=sum(r[2]>r[3] for r in rows);pa=round(100*wins/n,1);pb=round(100-pa,1)
        lines += [f'历史交锋胜率：{a} {pa:.1f}%｜{b} {pb:.1f}%',
                  f'样本：{n}场，{a} {wins}胜{n-wins}负；按整场系列赛计，不按单图计。',
                  '计算：该队获胜场数 ÷ 双方有效交锋总场数 × 100%。','采用的历史交锋（最多展示10场）：']
        for _,stamp,s1,s2,event in rows[:10]:
            lines.append(datetime.fromtimestamp(stamp,CST).strftime('%Y-%m-%d')+f' {a} {s1}:{s2} {b} · {event}')
        if n<5:lines.append('交锋样本少于5场，参考性有限。')
        lines.append('这是历史交锋频率，并非校准后的未来获胜概率；未考虑换人、地图池或近期状态，100%也不代表必胜。')
    lines.append('来源：完美世界公开赛事数据（近一年）；不调用AI计算百分比。\nhttps://data.wanmei.com/csgo/matches/'+str(match.id))
    return '\n'.join(lines)


def format_schedule(matches):
    if not matches:return '未来7天暂无双方已确定、且至少一队为当前 VRS 全球前十的未开始比赛。'
    lines=['未来7天赛程（VRS前十参赛，双方已确定，最多20场）']
    for i,m in enumerate(matches,1):
        lines.append(f'{i}. {m.team1.name} vs {m.team2.name}\n   '+datetime.fromtimestamp(m.start_time,CST).strftime('%m-%d %H:%M')+f' · {m.event} · ID {m.id}')
    lines.append('发送 /cs预测 2 或 /cs pre 2 查看第2场的交锋胜率。\n北京时间；本会话编号30分钟有效。')
    return '\n'.join(lines)
