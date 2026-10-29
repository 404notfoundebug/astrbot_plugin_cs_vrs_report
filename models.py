from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


class DataError(RuntimeError):
    pass


class AccessBlocked(DataError):
    def __init__(self, message: str, cooldown: int = 3600):
        super().__init__(message)
        self.cooldown = cooldown


def clean(value, limit=80):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or '')).strip()[:limit]


def integer(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() and 0 <= number < 10**12 else None
    except (ValueError, TypeError):
        return None


def decimal(value, maximum=1000):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= maximum else None
    except (ValueError, TypeError):
        return None


def epoch(value):
    number = decimal(value, 10**15)
    if number is None or number == 0:
        return None
    return number / 1000 if number > 10**11 else number


@dataclass
class Team:
    id: int
    name: str


@dataclass
class Ranking:
    teams: list[dict]
    source: str
    fetched_at: float
    source_date: str = ''

    def validate(self, limit=10):
        top = [t for t in self.teams if isinstance(t.get('rank'), int) and 1 <= t['rank'] <= limit]
        if {t['rank'] for t in top} != set(range(1,limit+1)) or len({t['id'] for t in top}) != limit:
            raise DataError(f'VRS 全球前{limit}不完整或队伍 ID 重复，暂停读取。')
        self.teams = sorted(top, key=lambda t:t['rank'])
        return self

    def rank_of(self, team_id):
        return next((t['rank'] for t in self.teams if t['id']==team_id), None)


@dataclass
class Match:
    id: int
    team1: Team
    team2: Team
    score1: int
    score2: int
    event: str
    end_time: float | None
    source: str
    start_time: float | None = None

    def relevant(self, ranking):
        return ranking.rank_of(self.team1.id) is not None or ranking.rank_of(self.team2.id) is not None


@dataclass
class Player:
    id: int
    name: str
    team_id: int
    kills: int | None
    deaths: int | None
    rating: float | None
    adr: float | None = None
    kast: float | None = None

    @property
    def kd(self):
        if self.kills is None or self.deaths is None:
            return '—'
        if not self.deaths:
            return '∞' if self.kills else '—'
        return f'{self.kills/self.deaths:.2f}'


@dataclass
class Report:
    match: Match
    players: list[Player] = field(default_factory=list)
    maps: list[dict] = field(default_factory=list)
    rating_label: str = 'Rating（原站）'
    source_url: str = ''
    scope: str = '全场'

    def complete(self):
        for team in (self.match.team1, self.match.team2):
            rows = [p for p in self.players if p.team_id==team.id]
            if not 5 <= len(rows) <= 8 or len({p.id for p in rows}) != len(rows):
                return False
            if any(p.rating is None or p.kills is None or p.deaths is None for p in rows):
                return False
        return True

    def payload(self):
        return asdict(self)


def format_report(report: Report, ranking: Ranking | None = None, review: str = ''):
    m=report.match
    lines=[f'🏆 CS2 赛果｜{m.event}',f'{m.team1.name} {m.score1}:{m.score2} {m.team2.name}']
    if ranking:
        ranks=[f'{t.name} VRS #{ranking.rank_of(t.id)}' for t in (m.team1,m.team2) if ranking.rank_of(t.id)]
        lines.append(' · '.join(ranks))
    if report.maps:
        lines.append('｜'.join(f'{x["name"]} {x["score1"]}:{x["score2"]}' for x in report.maps))
    lines.extend(['',f'选手统计：{report.scope} · {report.rating_label}','选手 | K-D | K/D | Rating'])
    for team in (m.team1,m.team2):
        lines.append(f'【{team.name}】')
        players=sorted([p for p in report.players if p.team_id==team.id], key=lambda p:p.rating if p.rating is not None else -1, reverse=True)
        for p in players:
            kd='—' if p.kills is None or p.deaths is None else f'{p.kills}-{p.deaths}'
            rating=f'{p.rating:.2f}' if p.rating is not None else '—'
            lines.append(f'{p.name} | {kd} | {p.kd} | {rating}')
        if not players:
            lines.append('选手统计暂未发布')
    if not report.complete():
        lines.append('⚠ 统计尚不完整；未将单图数据或平均 Rating 充当全场统计。')
    if review:
        lines.extend(['',f'🐟 大肥鱼点评：{review}'])
    lines.extend(['',f'数据：{m.source}',report.source_url])
    if ranking:
        label=ranking.source_date or datetime.fromtimestamp(ranking.fetched_at,CST).strftime('%m-%d %H:%M 采集')
        lines.append(f'排名：{ranking.source}｜{label}')
    return '\n'.join(lines)


def factual_review(report):
    rows=[p for p in report.players if p.rating is not None]
    if not rows:
        return '本鱼目前只拿到比分，选手表现等完整数据出来再评。'
    best=max(rows,key=lambda p:p.rating)
    return f'{best.name} 的 Rating {best.rating:.2f} 为本场所列选手最高，K-D {best.kills}-{best.deaths}。哼，这份数据本鱼认可了。'
