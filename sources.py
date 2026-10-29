from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from .models import AccessBlocked, CST, DataError, Match, Player, Ranking, Report, Team, clean, decimal, epoch, integer

HLTV='https://www.hltv.org'
PW_WEB='https://data.wanmei.com'
PW_API='https://esports.wanmei.com'
ALLOWED={'www.hltv.org','data.wanmei.com','esports.wanmei.com'}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urllib.parse.urlsplit(newurl).hostname not in ALLOWED:
            raise DataError('数据源重定向到非预期站点，已停止请求。')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Http:
    def __init__(self):
        self.opener=urllib.request.build_opener(SafeRedirect())
        self.lock=asyncio.Lock()
        self.next_at=0.0

    def _get(self,url,headers):
        parts=urllib.parse.urlsplit(url)
        if parts.scheme!='https' or parts.hostname not in ALLOWED or parts.username:
            raise DataError('拒绝非预期的数据地址。')
        req=urllib.request.Request(url,headers={'User-Agent':'AstrBot-CS-VRS/0.1 (self-hosted public esports reports)',**(headers or {})})
        try:
            with self.opener.open(req,timeout=18) as response:
                raw=response.read(5*1024*1024+1)
                if len(raw)>5*1024*1024:
                    raise DataError('数据响应超过 5 MB，已停止读取。')
                return raw.decode('utf-8-sig',errors='replace'),response.url
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403):
                raise AccessBlocked(f'{parts.hostname} 拒绝访问（HTTP {exc.code}），一小时后再尝试。') from exc
            if exc.code==429:
                retry=integer(exc.headers.get('Retry-After')) or 3600
                raise AccessBlocked('数据源限流（429），已停止密集重试。',max(900,min(retry,21600))) from exc
            raise DataError(f'{parts.hostname} HTTP {exc.code}') from exc
        except (urllib.error.URLError,TimeoutError,OSError) as exc:
            raise DataError(f'{parts.hostname} 网络请求失败：{type(exc).__name__}') from exc

    async def get(self,url,headers=None):
        async with self.lock:
            await asyncio.sleep(max(0,self.next_at-time.monotonic()))
            try:
                return await asyncio.to_thread(self._get,url,headers)
            finally:
                self.next_at=time.monotonic()+.8


def parse_pw_ranking(data,now=None,limit=10):
    rows=data.get('result')
    if data.get('code')!=0 or not isinstance(rows,list):
        raise DataError('完美世界 VALVE 全球榜返回结构异常。')
    teams=[]
    for row in rows:
        rank=integer(row.get('rank'));team_id=integer(row.get('hltvId'))
        if rank and rank<=limit and team_id:
            teams.append({'id':team_id,'name':clean(row.get('teamName')),'rank':rank,'points':decimal(row.get('score'),100000)})
    return Ranking(teams,'完美世界 VALVE 全球榜（实时估算）',now or time.time()).validate(limit)


def pw_match(row):
    if integer(row.get('status'))!=3:
        raise DataError('比赛尚未结束，或状态不受支持。')
    first=row.get('team1DTO') or {};second=row.get('team2DTO') or {}
    a=integer(row.get('team1Id') or first.get('teamId'));b=integer(row.get('team2Id') or second.get('teamId'))
    mid=integer(row.get('matchId'));s1=integer(row.get('score1'));s2=integer(row.get('score2'))
    if not all((a,b,mid)) or s1 is None or s2 is None:
        raise DataError('完美世界赛果缺少比赛 ID、队伍 ID 或比分。')
    return Match(mid,Team(a,clean(first.get('name'))),Team(b,clean(second.get('name'))),s1,s2,
                 clean((row.get('csgoEventDTO') or {}).get('nameZh') or (row.get('csgoEventDTO') or {}).get('name') or 'CS2 职业赛事'),
                 epoch(row.get('endTime')),'完美世界赛事数据中心',epoch(row.get('startTime')))


def parse_pw_matches(data):
    try:
        rows=data['result']['matchResponse']['dtoList']
    except (KeyError,TypeError) as exc:
        raise DataError('完美世界赛程返回结构异常。') from exc
    result=[]
    for row in rows:
        if integer(row.get('status'))==3:
            result.append(pw_match(row))
    return result


def parse_pw_report(data,map_index=0):
    try:
        row=data['result']['match']
    except (KeyError,TypeError) as exc:
        raise DataError('完美世界比赛详情结构异常。') from exc
    match=pw_match(row)
    groups=row.get('statsDTOList') or []
    maps=[];individual=[]
    for group in groups:
        name=clean((group.get('mapDTO') or {}).get('nameEn')).lower()
        if name and name!='all':
            individual.append(group)
            if integer(group.get('score1')) is not None and integer(group.get('score2')) is not None:
                maps.append({'name':name.title(),'score1':integer(group['score1']),'score2':integer(group['score2'])})
    if map_index:
        if not 1<=map_index<=len(individual):
            raise DataError('该场比赛没有这个图序号，或单图统计暂未发布。')
        selected=individual[map_index-1]
        scope=f'第 {map_index} 图 · {clean((selected.get("mapDTO") or {}).get("nameEn"))}'
    else:
        selected=next((g for g in groups if str((g.get('mapDTO') or {}).get('nameEn','')).lower()=='all'),{})
        scope='全场'
    players=[]
    for side,team in [(1,match.team1),(2,match.team2)]:
        for item in selected.get(f'team{side}PlayerStatsDTOList') or []:
            person=item.get('playerDTO') or {}
            if person.get('coach') is True:
                continue
            pid=integer(item.get('playerId') or person.get('playerId'))
            if not pid:
                continue
            players.append(Player(pid,clean(person.get('name'),40),team.id,integer(item.get('kills')),integer(item.get('deaths')),
                                  decimal(item.get('rating'),10),decimal(item.get('adr')),decimal(item.get('kast'),100)))
    return Report(match,players,maps,'Rating（完美世界原站）',f'{PW_WEB}/csgo/matches/{match.id}',scope)


class PerfectWorld:
    name='perfectworld'

    def __init__(self,http):
        self.http=http;self.public_key='';self.key_until=0

    async def _key(self):
        # Uses the public website's normal visitor protocol. No account token,
        # login state, CAPTCHA, private stats or native-app secret is used.
        if self.public_key and time.time()<self.key_until:
            return self.public_key
        html,_=await self.http.get(PW_WEB+'/')
        soup=BeautifulSoup(html,'html.parser')
        sources=[urllib.parse.urljoin(PW_WEB+'/',s.get('src','')) for s in soup.select('script[src]')]
        url=next((s for s in sources if urllib.parse.urlsplit(s).hostname=='data.wanmei.com' and re.search(r'/asset/csgo/js/csgo-[\w-]+\.js$',s)),None)
        if not url:
            raise DataError('完美世界网页入口已变更，未找到公开数据客户端。')
        script,_=await self.http.get(url)
        match=re.search(r'\bNp="([a-f0-9]{64})"',script)
        if not match or 'X-Signature' not in script or 'HmacSHA256' not in script:
            raise DataError('完美世界公开请求协议已变更，需要更新插件。')
        self.public_key=match.group(1);self.key_until=time.time()+86400
        return self.public_key

    async def api(self,path,params):
        key=await self._key();stamp=str(int(time.time()));nonce=secrets.token_hex(16)
        pairs=[]
        for k,v in {**params,'timestamp':stamp,'nonce':nonce}.items():
            for item in (v if isinstance(v,list) else [v]):pairs.append((str(k),str(item)))
        canonical=urllib.parse.urlencode(sorted(pairs))
        signature=hmac.new(key.encode(),canonical.encode(),hashlib.sha256).hexdigest()
        query=urllib.parse.urlencode(params,doseq=True)
        text,_=await self.http.get(PW_API+'/apg/eventcenter/csgo/'+path+'?'+query,
                                   {'X-Timestamp':stamp,'X-Nonce':nonce,'X-Signature':signature,'Origin':PW_WEB,'Referer':PW_WEB+'/'})
        try:
            data=json.loads(text)
        except ValueError as exc:
            raise DataError('完美世界未返回 JSON 数据。') from exc
        if data.get('code') not in (0,1):
            raise DataError(f'完美世界数据接口返回错误码 {data.get("code")}。')
        return data

    async def ranking(self):
        return parse_pw_ranking(await self.api('team/rank',{'page':1,'pageSize':10,'type':0}))

    async def world_ranking(self):
        rank=parse_pw_ranking(await self.api('team/rank',{'page':1,'pageSize':15,'type':0}),limit=15)
        if any(t.get('points') is None for t in rank.teams):raise DataError('VRS前15积分不完整，请稍后重试。')
        return rank

    async def results(self,ranking,days=2):
        now=datetime.now(CST);matches={}
        for offset in range(days):
            date=(now-timedelta(days=offset)).strftime('%Y-%m-%d')
            data=await self.api('getMatchList',{'matchTime':date+' 00:00:00'})
            for match in parse_pw_matches(data):matches[match.id]=match
        return list(matches.values())

    async def upcoming(self):
        from .prediction import scheduled_match
        now=datetime.now(CST);matches={}
        for offset in range(7):
            data=await self.api('getMatchList',{'matchTime':(now+timedelta(days=offset)).strftime('%Y-%m-%d')+' 00:00:00'})
            try:rows=data['result']['matchResponse']['dtoList']
            except (KeyError,TypeError) as exc:raise DataError('赛程结构异常。') from exc
            for row in rows:
                try:match=scheduled_match(row)
                except DataError:continue
                if match.start_time<now.timestamp()+7*86400:matches[match.id]=match
        return list(matches.values())

    async def scheduled(self,match_id):
        from .prediction import scheduled_match
        data=await self.api('getMatchDetail',{'matchId':int(match_id)})
        try:return scheduled_match(data['result']['match'])
        except (KeyError,TypeError) as exc:raise DataError('比赛详情缺失。') from exc

    async def head_to_head(self,a,b):
        return await self.api('team/recent/opponent/matches',{'teamId':a,'opponentTeamId':b})

    async def report(self,match_id,map_index=0):
        return parse_pw_report(await self.api('getMatchDetail',{'matchId':int(match_id)}),map_index)


def soup(html):
    if re.search(r'Just a moment|cf-chl-|verify you are human',html,re.I):
        raise AccessBlocked('HLTV 要求验证，已停止自动请求；可以切换完美世界。')
    return BeautifulSoup(html,'html.parser')


def parse_hltv_ranking(html,url,now=None):
    page=soup(html)
    if '/valve-ranking/teams' not in url or not re.search(r'Valve global ranking',page.get_text(' ',strip=True),re.I):
        raise DataError('未取得 HLTV 的 VALVE 全球榜，拒绝使用普通 HLTV 榜或区域榜。')
    teams=[]
    for row in page.select('.ranked-team'):
        pos=row.select_one('.position');link=row.select_one('a[href^="/team/"]');name=row.select_one('.name')
        if not pos or not link:continue
        rank=integer(pos.get_text(strip=True).lstrip('#'));match=re.search(r'/team/(\d+)',link.get('href',''))
        if rank and rank<=10 and match:
            teams.append({'id':int(match.group(1)),'rank':rank,'name':clean(name.get_text(strip=True) if name else link.get_text(strip=True))})
    date=re.search(r'/teams/(\d{4}/[a-z]+/\d+)',url)
    return Ranking(teams,'HLTV 实时估算 VRS 全球榜',now or time.time(),date.group(1) if date else '').validate()


def normalized(name):
    value=re.sub(r'[^a-z0-9]','',name.lower())
    return {'navi':'natusvincere','teamspirit':'spirit','betboom':'bb'}.get(value,value)


def parse_hltv_results(html,ranking):
    page=soup(html);matches={};by_name={normalized(t['name']):t for t in ranking.teams}
    rows=page.select('.result-con')
    if not rows and not page.select('.results-all'):
        raise DataError('HLTV 赛果页结构变化。')
    for row in rows:
        link=row.select_one('a[href^="/matches/"]');names=row.select('div.team');score=row.select_one('.result-score')
        if not link or len(names)!=2 or not score:continue
        mid=re.search(r'/matches/(\d+)/',link['href']);numbers=re.fullmatch(r'\s*(\d+)\s*[-:]\s*(\d+)\s*',score.get_text())
        if not mid or not numbers:continue
        teams=[clean(x.get_text(strip=True)) for x in names]
        ids=[by_name.get(normalized(n),{}).get('id',0) for n in teams]
        # IDs are verified on the detailed match page before any publication.
        match=Match(int(mid.group(1)),Team(ids[0],teams[0]),Team(ids[1],teams[1]),int(numbers[1]),int(numbers[2]),'',None,'HLTV',epoch(row.get('data-zonedgrouping-entry-unix')))
        matches[match.id]=match
    return list(matches.values())


def parse_hltv_match(html,match_id):
    page=soup(html)
    status=page.select_one('.countdown')
    if not status or 'Match over' not in status.get_text():
        raise DataError('HLTV 尚未确认比赛结束。')
    teams=[];scores=[]
    for side in (1,2):
        box=page.select_one(f'.team{side}-gradient')
        link=box.select_one('a[href^="/team/"]') if box else None
        name=box.select_one('.teamName') if box else None
        score=box.select_one('.won, .lost, .tie') if box else None
        tid=re.search(r'/team/(\d+)',link['href']) if link else None
        if not tid or not name or not score or integer(score.get_text(strip=True)) is None:
            raise DataError('HLTV 比赛队伍或比分结构变化。')
        teams.append(Team(int(tid.group(1)),clean(name.get_text(strip=True))));scores.append(integer(score.get_text(strip=True)))
    event=page.select_one('.timeAndEvent .event a')
    date=page.select_one('.timeAndEvent .date[data-unix]')
    match=Match(int(match_id),*teams,*scores,clean(event.get_text(strip=True) if event else 'CS2 职业赛事'),None,'HLTV',epoch(date.get('data-unix')) if date else None)
    maps=[]
    for box in page.select('.mapholder'):
        name=box.select_one('.mapname');left=box.select_one('.results-left .results-team-score');right=box.select_one('.results-right .results-team-score')
        if name and left and right and integer(left.get_text(strip=True)) is not None and integer(right.get_text(strip=True)) is not None:
            link=box.select_one('.results-stats')
            maps.append({'name':clean(name.get_text(strip=True)),'score1':integer(left.get_text(strip=True)),'score2':integer(right.get_text(strip=True)),
                         'stats_url':urllib.parse.urljoin(HLTV,link.get('href','')) if link else ''})
    detailed=next((a.get('href') for a in page.select('a[href]') if re.match(r'^/stats/matches/\d+/',a['href'])),None)
    return match,maps,urllib.parse.urljoin(HLTV,detailed) if detailed else ''


def parse_hltv_players(html,match):
    page=soup(html);tables=page.select('table.stats-table.totalstats')
    if len(tables)!=2:
        raise DataError('未找到明确的双方全场统计表，不猜测 All/T/CT 数据。')
    players=[];labels=[]
    for table in tables:
        header=table.select_one('thead') or table.select_one('tr')
        cells=header.select('th,td') if header else []
        headers=[c.get_text(' ',strip=True) for c in cells]
        kd=next((i for i,t in enumerate(headers) if re.fullmatch(r'K\s*[-/]\s*D',t,re.I)),None)
        kills=next((i for i,t in enumerate(headers) if t.lower() in ('k','kills')),None)
        deaths=next((i for i,t in enumerate(headers) if t.lower() in ('d','deaths')),None)
        rating=next((i for i,t in enumerate(headers) if 'rating' in t.lower()),None)
        if rating is None or (kd is None and (kills is None or deaths is None)):
            raise DataError('HLTV 统计列结构变化，未识别 Rating 或 K-D。')
        labels.append(headers[rating])
        name=header.get_text(' ',strip=True)
        team=next((t for t in (match.team1,match.team2) if normalized(t.name) in normalized(name)),None)
        if team is None:raise DataError('无法核验统计表对应哪支队伍。')
        for row in table.select('tbody tr') or table.select('tr')[1:]:
            columns=row.select('td');person=row.select_one('a[href*="/players/"]')
            if not person or len(columns)<=rating:continue
            pid=re.search(r'/players/(\d+)',person.get('href',''))
            if not pid:continue
            if kd is not None:
                pair=re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*',columns[kd].get_text())
                k,d=(int(pair[1]),int(pair[2])) if pair else (None,None)
            else:
                k=integer(columns[kills].get_text(strip=True));d=integer(columns[deaths].get_text(strip=True))
            nickname=person.select_one('.nick, .player-nick')
            players.append(Player(int(pid.group(1)),clean(nickname.get_text(strip=True) if nickname else person.get_text(strip=True),40),team.id,k,d,decimal(columns[rating].get_text(strip=True),10)))
    return players,' / '.join(dict.fromkeys(labels))+'（HLTV）'


class Hltv:
    name='hltv'
    def __init__(self,http):self.http=http

    async def ranking(self):
        text,url=await self.http.get(HLTV+'/valve-ranking/teams')
        return parse_hltv_ranking(text,url)

    async def results(self,ranking,days=2):
        text,_=await self.http.get(HLTV+'/results')
        return parse_hltv_results(text,ranking)

    async def report(self,match_id,map_index=0):
        text,_=await self.http.get(f'{HLTV}/matches/{int(match_id)}/-')
        match,maps,stats_url=parse_hltv_match(text,match_id)
        scope='全场'
        if map_index:
            if not 1<=map_index<=len(maps):raise DataError('该图暂无统计。')
            stats_url=maps[map_index-1].get('stats_url');scope=f'第 {map_index} 图 · {maps[map_index-1]["name"]}'
        if not stats_url:raise DataError('HLTV 选手统计尚未发布。')
        if not re.match(r'^https://www\.hltv\.org/stats/matches/(?:mapstatsid/)?\d+/',stats_url):
            raise DataError('HLTV 统计地址不受支持。')
        text,_=await self.http.get(stats_url)
        players,label=parse_hltv_players(text,match)
        return Report(match,players,maps,label,f'{HLTV}/matches/{match.id}/-',scope)


class Sources:
    def __init__(self,mode='auto',http=None):
        self.mode=mode;http=http or Http();self.providers=[PerfectWorld(http),Hltv(http)]
        self.blocked={};self.last_errors={};self.ranking_cache=None;self.ranking_cache_mode=None

    async def call(self,method,*args):
        errors=[]
        for provider in self.providers:
            if self.mode!='auto' and provider.name!=self.mode:continue
            if time.time()<self.blocked.get(provider.name,0):
                errors.append(provider.name+' 仍在退避');continue
            if not hasattr(provider,method):
                errors.append(provider.name+' 暂不支持此功能，请使用完美世界来源');continue
            try:
                result=await getattr(provider,method)(*args)
                if method=='report' and not result.complete():
                    raise DataError(provider.name+' 选手统计尚不完整。')
                self.last_errors.pop(provider.name,None)
                return result
            except AccessBlocked as exc:
                self.blocked[provider.name]=time.time()+exc.cooldown
                self.last_errors[provider.name]=str(exc);errors.append(str(exc))
            except (DataError,ValueError,KeyError,TypeError) as exc:
                self.last_errors[provider.name]=str(exc);errors.append(str(exc))
        raise DataError('；'.join(errors) or '未配置可用数据源。')

    async def ranking(self):
        if self.ranking_cache and self.ranking_cache_mode==self.mode and time.time()-self.ranking_cache.fetched_at<3600:
            return self.ranking_cache
        self.ranking_cache=await self.call('ranking')
        self.ranking_cache_mode=self.mode
        return self.ranking_cache

    async def results(self,ranking):return await self.call('results',ranking,2)
    async def report(self,match_id,map_index=0):return await self.call('report',match_id,map_index)

    async def upcoming(self):return await self.call('upcoming')
    async def scheduled(self,match_id):return await self.call('scheduled',match_id)
    async def head_to_head(self,a,b):return await self.call('head_to_head',a,b)

    async def world_ranking(self):return await self.call('world_ranking')
