from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for
from flask import abort

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "tama.db"

app = Flask(__name__)

STAT_KEYS = ["strength", "stamina", "intelligence", "wealth", "discipline", "consistency"]
ACTION_TYPES = ["strength", "stamina", "intelligence", "wealth", "discipline"]


TYPE_KO = {
    "strength": "근력운동",
    "stamina": "유산소",
    "intelligence": "공부/독서",
    "wealth": "적금/투자",
    "discipline": "금주/식단",
}

GAIN = {
    "strength": 5,        # 근력
    "stamina": 5,         # 유산소
    "intelligence": 3,    # 공부
    "wealth": 7,          # 적금/투자 성공
    "discipline": 4,      # 금주/식단
}
EXP_PER_ACTION = 10
CONSISTENCY_PER_DAY = 2

def need_exp_for_next(level: int) -> int:
    return 100 + (level * 20)

def streak_bonus_exp(streak: int) -> int:
    # "그날 1개 이상 입력" 연속일수 기준 보너스 (EXP만)
    if streak >= 30:
        return 30
    if streak >= 10:
        return 30
    if streak >= 5:
        return 15
    if streak >= 2:
        return 5
    return 0

def today_str() -> str:
    return date.today().isoformat()

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY,
        level INTEGER NOT NULL,
        exp INTEGER NOT NULL,
        strength INTEGER NOT NULL,
        stamina INTEGER NOT NULL,
        intelligence INTEGER NOT NULL,
        wealth INTEGER NOT NULL,
        discipline INTEGER NOT NULL,
        consistency INTEGER NOT NULL,
        streak INTEGER NOT NULL,
        last_check_date TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        type TEXT NOT NULL,
        amount INTEGER NOT NULL DEFAULT 1,
        note TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_summary (
        date TEXT PRIMARY KEY,
        did_anything INTEGER NOT NULL,
        streak_after INTEGER NOT NULL
    )
    """)

    # create default profile (single user)
    cur.execute("SELECT COUNT(*) AS c FROM profile")
    if cur.fetchone()["c"] == 0:
        cur.execute("""
        INSERT INTO profile (id, level, exp, strength, stamina, intelligence, wealth, discipline, consistency, streak, last_check_date)
        VALUES (1, 1, 0, 0, 0, 0, 0, 0, 0, 0, ?)
        """, (today_str(),))
    conn.commit()
    conn.close()

def get_profile():
    conn = db()
    row = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
    conn.close()
    return dict(row)

def update_profile(p: dict):
    conn = db()
    conn.execute("""
    UPDATE profile
    SET level=?, exp=?, strength=?, stamina=?, intelligence=?, wealth=?, discipline=?, consistency=?, streak=?, last_check_date=?
    WHERE id=1
    """, (
        p["level"], p["exp"],
        p["strength"], p["stamina"], p["intelligence"], p["wealth"], p["discipline"], p["consistency"],
        p["streak"], p["last_check_date"]
    ))
    conn.commit()
    conn.close()

def did_anything_on(d: str) -> bool:
    conn = db()
    row = conn.execute("SELECT 1 FROM actions WHERE date=? LIMIT 1", (d,)).fetchone()
    conn.close()
    return row is not None

def get_recent_actions(limit: int = 30):
    conn = db()
    rows = conn.execute("""
        SELECT id, date, type, note
        FROM actions
        ORDER BY date DESC, id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    out = []
    for r in rows:
        t = r["type"]
        out.append({
            "id": r["id"],
            "date": r["date"],
            "type": t,
            "type_ko": TYPE_KO.get(t, t),  # <-- 이게 핵심
            "note": r["note"] or ""
        })
    return out

@app.route("/delete", methods=["POST"])
def delete_action():
    init_db()
    action_id = request.form.get("action_id", "").strip()
    if not action_id.isdigit():
        return redirect(url_for("index"))

    conn = db()
    conn.execute("DELETE FROM actions WHERE id=?", (int(action_id),))
    conn.commit()
    conn.close()

    # ✅ 삭제 후 profile 재계산
    new_p = recompute_profile_from_actions()
    save_profile_dict(new_p)

    return redirect(url_for("index"))

@app.route("/edit/<int:action_id>", methods=["GET"])
def edit_action(action_id: int):
    init_db()
    conn = db()
    row = conn.execute("SELECT id, date, type, note FROM actions WHERE id=?", (action_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)

    action = dict(row)
    return render_template(
        "edit.html",
        action=action,
        type_ko=TYPE_KO,
        action_types=list(TYPE_KO.keys())
    )


@app.route("/update", methods=["POST"])
def update_action():
    init_db()
    action_id = request.form.get("action_id", "").strip()
    new_date = request.form.get("date", "").strip()
    new_type = request.form.get("type", "").strip()
    new_note = request.form.get("note", "").strip()

    if not action_id.isdigit():
        return redirect(url_for("index"))
    if new_type not in TYPE_KO:
        return redirect(url_for("index"))
    # date는 YYYY-MM-DD 형식만 허용(간단 검증)
    try:
        _ = parse_date(new_date)
    except Exception:
        return redirect(url_for("index"))

    conn = db()
    conn.execute(
        "UPDATE actions SET date=?, type=?, note=? WHERE id=?",
        (new_date, new_type, new_note if new_note else None, int(action_id))
    )
    conn.commit()
    conn.close()

    # ✅ 수정 후 profile 재계산
    new_p = recompute_profile_from_actions()
    save_profile_dict(new_p)

    return redirect(url_for("index"))




# ====== 재계산 함수 ============
def get_all_actions():
    conn = db()
    rows = conn.execute("""
        SELECT date, type
        FROM actions
        ORDER BY date ASC, id ASC
    """).fetchall()
    conn.close()
    return [(r["date"], r["type"]) for r in rows]


def recompute_profile_from_actions():
    """
    actions 테이블을 '진실의 원천'으로 삼아 profile을 통째로 재계산한다.
    규칙:
    - 행동 1개당: 해당 스탯 +GAIN[type], EXP +10
    - 하루에 기록이 1개 이상이면: consistency +2, streak +1, streak 보너스 EXP
    - 약한 감쇠: 현재 날짜 기준 최근 7일 동안 해당 행동이 없으면 해당 스탯 -2
    - 레벨업: while exp >= need(level)
    """
    actions = get_all_actions()
    today = today_str()

    # 초기화
    p = {
        "level": 1,
        "exp": 0,
        "strength": 0,
        "stamina": 0,
        "intelligence": 0,
        "wealth": 0,
        "discipline": 0,
        "consistency": 0,
        "streak": 0,
        "last_check_date": today,
    }

    if not actions:
        return p

    # 날짜별로 묶기
    by_date = {}
    for d, t in actions:
        by_date.setdefault(d, []).append(t)

    # actions에 등장한 첫 날짜부터 today까지 하루씩 진행(공백일 포함 → streak 정확)
    start = parse_date(min(by_date.keys()))
    end = parse_date(today)

    d = start
    while d <= end:
        d_str = d.isoformat()
        types_today = by_date.get(d_str, [])

        # 그날 기록 처리(행동 스탯/EXP)
        for t in types_today:
            if t in GAIN:
                p[t] += GAIN[t]
                p["exp"] += EXP_PER_ACTION

        # streak/consistency는 "하루에 1개 이상 기록" 기준
        if len(types_today) > 0:
            p["consistency"] += CONSISTENCY_PER_DAY
            p["streak"] += 1
            p["exp"] += streak_bonus_exp(p["streak"])
        else:
            p["streak"] = 0

        # 레벨업 처리(매일 처리해도 되고, 마지막에 몰아도 되는데 일관성 위해 여기서 처리)
        while p["exp"] >= need_exp_for_next(p["level"]):
            p["exp"] -= need_exp_for_next(p["level"])
            p["level"] += 1

        d += timedelta(days=1)

    # 감쇠(현재 날짜 기준, 최근 7일 동안 행동 없으면 -2)
    # last action date를 actions에서 계산
    last_date_for = {t: None for t in ACTION_TYPES}
    for d_str, t in actions:
        if t in last_date_for:
            last_date_for[t] = d_str

    cur_d = parse_date(today)
    for t in ACTION_TYPES:
        last_d_str = last_date_for.get(t)
        if not last_d_str:
            continue
        last_d = parse_date(last_d_str)
        if (cur_d - last_d).days >= 7:
            p[t] = max(0, p[t] - 2)

    return p


def save_profile_dict(p: dict):
    # profile row를 통째로 업데이트
    conn = db()
    conn.execute("""
        UPDATE profile
        SET level=?, exp=?, strength=?, stamina=?, intelligence=?, wealth=?, discipline=?, consistency=?, streak=?, last_check_date=?
        WHERE id=1
    """, (
        p["level"], p["exp"],
        p["strength"], p["stamina"], p["intelligence"], p["wealth"], p["discipline"], p["consistency"],
        p["streak"], p["last_check_date"],
    ))
    conn.commit()
    conn.close()

def last_action_date_for_type(t: str) -> str | None:
    conn = db()
    row = conn.execute("""
        SELECT date FROM actions
        WHERE type=?
        ORDER BY date DESC
        LIMIT 1
    """, (t,)).fetchone()
    conn.close()
    return row["date"] if row else None

def apply_weekly_decay(p: dict, current_date: str):
    """
    약한 감쇠:
    최근 7일 동안 해당 타입 행동이 0회면 해당 스탯 -2 (최저 0)
    """
    cur_d = parse_date(current_date)
    for t in ACTION_TYPES:
        last_d_str = last_action_date_for_type(t)
        if not last_d_str:
            continue
        last_d = parse_date(last_d_str)
        if (cur_d - last_d).days >= 7:
            p[t] = max(0, p[t] - 2)

def compute_class_and_traits(p: dict):
    # 1위 스탯(6개 중) 기준 클래스
    stats = {k: p[k] for k in STAT_KEYS}
    sorted_stats = sorted(stats.items(), key=lambda kv: kv[1], reverse=True)
    top1, v1 = sorted_stats[0]
    top2, v2 = sorted_stats[1]
    top3, v3 = sorted_stats[2]

    base_class = {
        "strength": "보디빌더",
        "stamina": "러너",
        "intelligence": "학자",
        "wealth": "재력가",
        "discipline": "현자",
        "consistency": "맑은눈",  # 꾸준함 1위면 맑은눈 성향
    }[top1]

    # 멀티 특성: 2위가 1위의 70% 이상이면 활성화 (v1=0이면 예외)
    trait = None
    if v1 > 0 and v2 >= int(v1 * 0.7):
        trait = {
            "strength": "보디빌더",
            "stamina": "러너",
            "intelligence": "학자",
            "wealth": "재력가",
            "discipline": "현자",
            "consistency": "맑은눈",
        }[top2]

    # 맑은눈 확장형: 상위3개 차이가 10 이내면
    title = None
    if (v1 - v3) <= 10 and v1 > 0:
        title = "맑은눈(확장형)"

    return base_class, trait, title, sorted_stats

def layer_flags(p: dict):
    # MVP 기준 20/50 두 단계
    flags = {}
    for k in STAT_KEYS:
        flags[f"{k}_20"] = p[k] >= 20
        flags[f"{k}_50"] = p[k] >= 50
    return flags

def handle_daily_check(p: dict, current_date: str):
    """
    - 날짜가 바뀌면:
      - 어제 입력했는지로 streak 갱신
      - 감쇠 적용
      - last_check_date 갱신
    """
    last = parse_date(p["last_check_date"])
    cur = parse_date(current_date)
    if cur <= last:
        return p

    # 하루씩 지나간 것 처리(공백일 포함)
    d = last + timedelta(days=1)
    while d <= cur:
        d_str = d.isoformat()
        # streak는 "그날 1개 이상 입력" 기준
        if did_anything_on(d_str):
            p["streak"] += 1
        else:
            p["streak"] = 0
        # 감쇠는 현재 날짜 기준으로 판단(단순화: 체크 시점에만 한 번 적용)
        d = d + timedelta(days=1)

    # 감쇠 적용(현재 날짜 기준)
    apply_weekly_decay(p, current_date)
    p["last_check_date"] = current_date
    return p

def add_actions(date_str: str, selected: list[str], note: str | None):
    conn = db()
    for t in selected:
        conn.execute("INSERT INTO actions (date, type, amount, note) VALUES (?, ?, 1, ?)",
                     (date_str, t, note if note else None))
    conn.commit()
    conn.close()

@app.route("/", methods=["GET"])
def index():
    init_db()
    p = get_profile()

    # 날짜 체크(스트릭/감쇠)
    p = handle_daily_check(p, today_str())
    update_profile(p)

    base_class, trait, title, sorted_stats = compute_class_and_traits(p)
    flags = layer_flags(p)

    recent_actions = get_recent_actions(30)

    return render_template(
        "index.html",
        p=p,
        need=need_exp_for_next(p["level"]),
        base_class=base_class,
        trait=trait,
        title=title,
        sorted_stats=sorted_stats,
        flags=flags,
        recent_actions=recent_actions
    )

@app.route("/log", methods=["POST"])
def log_today():
    init_db()

    d = today_str()
    selected = request.form.getlist("actions")
    note = request.form.get("note", "").strip()

    if not selected:
        return redirect(url_for("index"))

    # 기록 저장 (스탯 계산은 여기서 하지 않음)
    conn = db()
    for t in selected:
        conn.execute(
            "INSERT INTO actions (date, type, amount, note) VALUES (?, ?, 1, ?)",
            (d, t, note if note else None)
        )
    conn.commit()
    conn.close()

    # ✅ 여기서 profile 전체 재계산
    new_p = recompute_profile_from_actions()
    save_profile_dict(new_p)

    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)