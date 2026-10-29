"""Read-only live check. No AstrBot import, LLM call or QQ message."""
import asyncio
import json

from .models import format_report
from .sources import Sources


async def run():
    source=Sources('perfectworld')
    ranking=await source.ranking()
    matches=await source.results(ranking)
    matches=sorted([m for m in matches if m.relevant(ranking)],key=lambda m:m.end_time or m.start_time or 0,reverse=True)
    print(json.dumps({'ranking_source':ranking.source,'top10':[(t['rank'],t['name']) for t in ranking.teams],
                      'finished_matches':len(matches)},ensure_ascii=False))
    if not matches:
        print('目前没有符合条件的已结束比赛。');return
    report=await source.report(matches[0].id)
    print(format_report(report,ranking,'这是只读连通性检查，未调用 AI。'))


if __name__=='__main__':asyncio.run(run())
