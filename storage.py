from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .models import Match, Ranking, Team


class Store:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS targets(umo TEXT PRIMARY KEY,active INTEGER,initialized INTEGER);
        CREATE TABLE IF NOT EXISTS jobs(
          umo TEXT,match_id INTEGER,status TEXT,first_seen REAL,updated REAL,retry_at REAL,
          match_json TEXT,ranking_json TEXT,error TEXT DEFAULT '',message TEXT DEFAULT '',
          PRIMARY KEY(umo,match_id));
        CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(status,retry_at);
        CREATE TABLE IF NOT EXISTS reviews(key TEXT PRIMARY KEY,text TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        ''')
        # A process may die after sending but before receiving the platform ack.
        # Hold these jobs for explicit inspection rather than duplicating posts.
        self.db.execute("UPDATE jobs SET status='uncertain',error='上次进程在发送期间退出，需人工确认是否已收到' WHERE status='sending'")
        self.db.commit()

    def sync_targets(self,targets):
        current={r['umo']:r for r in self.db.execute('SELECT * FROM targets')}
        for umo,row in current.items():
            if umo not in targets:self.db.execute('UPDATE targets SET active=0,initialized=0 WHERE umo=?',(umo,))
        for umo in targets:
            if umo not in current:self.db.execute('INSERT INTO targets VALUES(?,1,0)',(umo,))
            elif not current[umo]['active']:self.db.execute('UPDATE targets SET active=1,initialized=0 WHERE umo=?',(umo,))
        self.db.commit()

    def initialize_target(self,umo,matches,ranking,now):
        row=self.db.execute('SELECT initialized FROM targets WHERE umo=?',(umo,)).fetchone()
        if row and row['initialized']:return False
        for match in matches:
            self.add(umo,match,ranking,'baseline',now,commit=False)
            self.db.execute("UPDATE jobs SET status='baseline',updated=? WHERE umo=? AND match_id=? AND status NOT IN ('sent','uncertain')",(now,umo,match.id))
        self.db.execute('UPDATE targets SET initialized=1 WHERE umo=?',(umo,));self.db.commit()
        return True

    def add(self,umo,match,ranking,status,now,commit=True):
        self.db.execute('INSERT OR IGNORE INTO jobs(umo,match_id,status,first_seen,updated,retry_at,match_json,ranking_json) VALUES(?,?,?,?,?,?,?,?)',
                        (umo,match.id,status,now,now,now,json.dumps(asdict(match),ensure_ascii=False),json.dumps(asdict(ranking),ensure_ascii=False)))
        if commit:self.db.commit()

    def pending(self,targets,now,limit):
        if not targets:return []
        placeholders=','.join('?' for _ in targets)
        return [dict(r) for r in self.db.execute(f"SELECT * FROM jobs WHERE umo IN ({placeholders}) AND status='pending' AND retry_at<=? ORDER BY first_seen,match_id LIMIT ?",(*targets,now,limit))]

    def update(self,umo,mid,status,error='',retry_at=0,message=''):
        self.db.execute('UPDATE jobs SET status=?,updated=?,error=?,retry_at=?,message=CASE WHEN ?=\'\' THEN message ELSE ? END WHERE umo=? AND match_id=?',
                        (status,time.time(),error[:500],retry_at,message,message,umo,mid));self.db.commit()

    def retry_uncertain(self,umo,mid):
        cur=self.db.execute("UPDATE jobs SET status='pending',retry_at=0,error='' WHERE umo=? AND match_id=? AND status='uncertain'",(umo,mid))
        self.db.commit();return cur.rowcount>0

    def counts(self):
        return {r['status']:r['n'] for r in self.db.execute('SELECT status,count(*) n FROM jobs GROUP BY status')}

    def last_sent(self,umo):
        row=self.db.execute("SELECT message FROM jobs WHERE umo=? AND status='sent' ORDER BY updated DESC LIMIT 1",(umo,)).fetchone()
        return row['message'] if row else None

    def review(self,key):
        row=self.db.execute('SELECT text FROM reviews WHERE key=? AND updated>?',(key,time.time()-604800)).fetchone()
        return row['text'] if row else None

    def save_review(self,key,text):
        self.db.execute('INSERT OR REPLACE INTO reviews VALUES(?,?,?)',(key,text,time.time()));self.db.commit()

    def setting(self,key,default=None):
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        try:return json.loads(row['value']) if row else default
        except ValueError:return default

    def set_setting(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)));self.db.commit()

    def prune(self,now):
        # Retain IDs, even for skipped games, longer than any scan window.
        self.db.execute("DELETE FROM jobs WHERE updated<? AND status NOT IN ('pending','sending','uncertain')",(now-30*86400,))
        self.db.execute('DELETE FROM reviews WHERE updated<?',(now-7*86400,));self.db.commit()

    def close(self):self.db.close()


def restore_match(raw):
    data=json.loads(raw);data['team1']=Team(**data['team1']);data['team2']=Team(**data['team2'])
    return Match(**data)


def restore_ranking(raw):return Ranking(**json.loads(raw))
