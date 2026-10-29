"""Bounded, per-conversation list snapshots keep numbered selections stable."""
import time
from datetime import datetime
from .models import CST, DataError, clean


class MatchLists:
    def __init__(self, ttl=1800, limit=100):
        self.ttl, self.limit, self.items = ttl, limit, {}

    def save(self, umo, matches, ranking, upcoming=False):
        now = time.time()
        self.items = {k:v for k,v in self.items.items() if now-v[0] < self.ttl}
        self.items.pop(umo, None)
        while len(self.items) >= self.limit:
            self.items.pop(next(iter(self.items)))
        matches = sorted({m.id:m for m in matches if m.relevant(ranking)}.values(),
                         key=lambda m:(m.end_time or m.start_time or 0, m.id), reverse=not upcoming)[:20]
        self.items[umo] = (now, matches, ranking)
        return matches

    def select(self, umo, number):
        saved = self.items.get(umo)
        if saved is None or time.time()-saved[0] >= self.ttl:
            raise DataError('请先发送 /cs列表 获取本会话的比赛列表；编号有效期30分钟，插件重载后需重新获取。')
        _, matches, ranking = saved
        if not 1 <= number <= len(matches):
            raise DataError(f'编号超出范围，请选择1至{len(matches)}，或重新发送 /cs列表。')
        return matches[number-1], ranking


def format_list(matches):
    if not matches:
        return '近两天没有符合当前 VRS 全球前十条件的已结束比赛。'
    lines = ['近期赛果（任一队为当前 VRS 全球前十，最多20场）']
    for i, match in enumerate(matches, 1):
        stamp = datetime.fromtimestamp(match.end_time or match.start_time,CST).strftime('%m-%d %H:%M') if match.end_time or match.start_time else '时间待定'
        lines.append(f'{i}. {clean(match.team1.name)} {match.score1}:{match.score2} {clean(match.team2.name)}\n   {stamp} · {clean(match.event)} · ID {match.id}')
    lines.append('发送 /cs 2 查看第2场完整数据与点评。\n编号仅对应本会话最新列表，30分钟有效；时间为北京时间。')
    return '\n'.join(lines)
