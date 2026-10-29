from __future__ import annotations

import asyncio
import re
import time

from .models import DataError, format_report
from .storage import restore_match, restore_ranking


def valid_umo(value):
    return isinstance(value,str) and re.fullmatch(r'[^:\s]+:GroupMessage:[^:\s]+',value) is not None


class Engine:
    def __init__(self,store,sources,send,review,config):
        self.store=store;self.sources=sources;self.send=send;self.review=review;self.config=config
        self.lock=asyncio.Lock();self.last_check=0;self.last_error='';self.last_summary='等待首次检查'
        self.sources.blocked=self.store.setting('blocked_sources',{})
        self.report_cache={}

    async def report(self,mid,map_index=0):
        key=(self.sources.mode,int(mid),map_index);cached=self.report_cache.get(key)
        if cached and time.time()-cached[0]<1800:return cached[1]
        report=await self.sources.report(int(mid),map_index)
        if report.match.id!=int(mid):raise DataError('详情比赛 ID 与请求不一致，拒绝播报。')
        if not report.complete():raise DataError('选手统计尚不完整，请稍后再试。')
        self.report_cache[key]=(time.time(),report)
        if len(self.report_cache)>60:
            old=min(self.report_cache,key=lambda k:self.report_cache[k][0]);self.report_cache.pop(old,None)
        return report

    async def scan(self,targets):
        if any(not valid_umo(t) for t in targets):
            raise DataError('UMO 必须是完整的群聊来源，例如 qq-snowluma:GroupMessage:群号。')
        if not targets:
            self.last_summary='尚未填写群聊 UMO，不会主动抓取或发送。';return
        async with self.lock:
            self.last_check=time.time();self.store.sync_targets(targets)
            try:
                ranking=await self.sources.ranking();matches=await self.sources.results(ranking)
                now=time.time();new_targets=[]
                for umo in targets:
                    if self.store.initialize_target(umo,matches,ranking,now):
                        new_targets.append(umo);continue
                    for match in matches:
                        age=now-(match.end_time or now)
                        state='pending' if match.relevant(ranking) else 'ineligible'
                        if age>max(1,int(self.config.get('lookback_hours',24)))*3600 or age< -120:
                            state='expired'
                        self.store.add(umo,match,ranking,state,now)
                self.last_error=''
                self.last_summary=f'取得 {len(matches)} 场已结束比赛；新订阅基线 {len(new_targets)} 个。'
            except (DataError,OSError,ValueError) as exc:
                self.last_error=str(exc)
                raise
            finally:
                self.store.set_setting('blocked_sources',self.sources.blocked)
            ready=self.store.pending(targets,now,max(1,min(int(self.config.get('max_reports_per_poll',3)),10)))
            delivered=0
            for job in ready:
                umo=job['umo'];mid=job['match_id'];match=restore_match(job['match_json']);rank=restore_ranking(job['ranking_json'])
                delay=max(0,int(self.config.get('settle_minutes',5)))*60
                due=(match.end_time or job['first_seen'])+delay
                if now<due:
                    self.store.update(umo,mid,'pending',retry_at=due);continue
                if now-job['first_seen']>max(1,int(self.config.get('stats_wait_hours',6)))*3600:
                    self.store.update(umo,mid,'expired','等待选手数据超时，未发送不完整统计。');continue
                try:
                    report=await self.report(mid)
                    if not report.complete():raise DataError('选手统计暂不完整。')
                    if {report.match.team1.id,report.match.team2.id}!={match.team1.id,match.team2.id}:
                        # HLTV result rows may omit the non-top-ten opponent ID.
                        known={t.id for t in (match.team1,match.team2) if t.id}
                        actual={report.match.team1.id,report.match.team2.id}
                        if not known.issubset(actual):raise DataError('比赛队伍与赛果列表不一致。')
                    if not report.match.relevant(rank):raise DataError('详情队伍不在本次 VRS 前十筛选中。')
                    review=await self.review(report,umo)
                    message=format_report(report,rank,review)
                except Exception as exc:
                    self.store.update(umo,mid,'pending',str(exc),retry_at=time.time()+600)
                    self.last_error=f'比赛 {mid} 暂未播报：{type(exc).__name__}';continue
                # Durable intent before the platform call, not after it.
                self.store.update(umo,mid,'sending',message=message)
                try:
                    sent=await self.send(umo,message)
                    if sent is False:
                        self.store.update(umo,mid,'pending','未找到 UMO 对应平台，请检查平台实例 ID。',time.time()+600)
                    elif sent is True:
                        self.store.update(umo,mid,'sent',message=message);delivered+=1
                    else:
                        self.store.update(umo,mid,'uncertain','平台未返回明确发送确认，请人工检查。')
                except BaseException as exc:
                    self.store.update(umo,mid,'uncertain','发送结果不明：'+type(exc).__name__)
                    if isinstance(exc,asyncio.CancelledError):raise
                    self.last_error=f'比赛 {mid} 发送结果不明，已暂停该条重试。'
            self.store.prune(time.time());self.store.set_setting('blocked_sources',self.sources.blocked)
            self.last_summary+=f' 本轮成功发送 {delivered} 条。'
